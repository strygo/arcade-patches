"""Input pins: check that a rip carries exactly the audio a pack was built
from, and say precisely how it differs when it does not.

Every pack is verified against its published sha256, but a mismatch there
only says that something differs.  The album and CD-audio builders are
deterministic from their decoded input PCM, so the useful question is WHICH
input differs, and how.  manifests/<pack>_inputs.json pins every input track
of the verified build:

  frames   length in stereo sample frames
  sha256   of the decoded 16-bit PCM
  crc32    of the same PCM: what Exact Audio Copy logs as a track's Copy CRC
           (null samples included), and, for a CD track taken whole from a
           one-.bin-per-track image, that .bin's CRC32
  anchors  a few 4096-frame windows of the track, each kept only as its two
           per-channel sample sums and a truncated sha256 -- enough to FIND
           the recording inside another rip, and nothing from which any of
           the audio could be recovered
  blocks   CRC32 of each 5-second block, to say where two copies of the same
           recording part ways

and, for each track of each pack, the sha256 of the PCM handed to the ADX
encoder, of the ADX stream that came back, and the loop points -- so a pack
that does not verify can be traced to the first stage that differs.

When a track's PCM is not the pinned one, the rip is searched for the pinned
recording by its anchors.  A different read offset, a gap appended to the
neighbouring track instead of this one, a pregap left out, a disc ripped as
one image, or tags that number the tracks differently all still contain the
exact samples, just not where the track list says.  The pinned span is then
cut from wherever it was found -- across file boundaries if need be, with
anything before the first file or past the last one taken as digital
silence, which is what EAC fills an offset with -- and accepted only if its
sha256 is the pinned one.  Anything else is reported, never guessed.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import wave
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

FORMAT = 1
ANCHOR_FRAMES = 4096
BLOCK_FRAMES = 5 * 44100
SECTOR_FRAMES = 588                     # one CD sector of audio
_ANCHOR_AT = (0.12, 0.31, 0.5, 0.69, 0.88)
_SCAN_CHUNK = 1 << 21                   # frames per scan read (~47 s)


# --- decoding ------------------------------------------------------------------
# Album files are decoded exactly the way the builders decode them (one
# command line, shared); CD tracks are the WAVs discsrc extracted.

def ffmpeg_exe() -> str:
    return os.environ.get("CPSPLUS_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def ffmpeg_s16_cmd(path: Path) -> list[str]:
    """The album decode: first audio stream, stereo s16le at 44.1 kHz."""
    return [ffmpeg_exe(), "-v", "error", "-i", str(path), "-f", "s16le",
            "-acodec", "pcm_s16le", "-ac", "2", "-ar", "44100", "-"]


def _iter_ffmpeg(path: Path, chunk: int):
    proc = subprocess.Popen(ffmpeg_s16_cmd(path), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    assert proc.stdout is not None
    want, finished = chunk * 4, False
    try:
        while True:
            buf = proc.stdout.read(want)
            if not buf:
                break
            if len(buf) % 4:
                buf += proc.stdout.read(4 - len(buf) % 4)
            yield np.frombuffer(buf[:len(buf) // 4 * 4], "<i2").reshape(-1, 2)
        finished = True
    finally:
        if not finished:                 # the caller stopped reading early
            proc.kill()
        proc.stdout.close()
        err = proc.stderr.read() if proc.stderr else b""
        proc.stderr.close()
        if proc.wait() and finished:
            raise RuntimeError(f"ffmpeg could not decode {path.name}: "
                               f"{err.decode(errors='replace')[:300]}")


def _iter_wav(path: Path, chunk: int):
    with wave.open(str(path), "rb") as w:
        if (w.getsampwidth(), w.getnchannels()) != (2, 2):
            raise ValueError(f"{path.name}: expected 16-bit stereo")
        while True:
            buf = w.readframes(chunk)
            if not buf:
                break
            yield np.frombuffer(buf, "<i2").reshape(-1, 2)


def iter_pcm(path: Path, reader: str, chunk: int = _SCAN_CHUNK):
    """(frames, 2) int16 chunks of one file.  reader: "ffmpeg" or "wav"."""
    return (_iter_wav if reader == "wav" else _iter_ffmpeg)(Path(path), chunk)


def read_pcm(path: Path, reader: str) -> np.ndarray:
    """A whole file as interleaved int16 (the builders' shape)."""
    parts = [c.reshape(-1) for c in iter_pcm(path, reader)]
    return (np.concatenate(parts) if parts else np.zeros(0, "<i2")).astype("<i2")


def read_range(path: Path | None, reader: str, start: int, count: int) -> np.ndarray:
    """Frames [start, start+count) of a file as (count, 2); None is silence."""
    if path is None or count <= 0:
        return np.zeros((max(count, 0), 2), "<i2")
    out, pos = [], 0
    for c in iter_pcm(path, reader):
        lo, hi = max(start - pos, 0), min(start + count - pos, len(c))
        if lo < hi:
            out.append(c[lo:hi])
        pos += len(c)
        if pos >= start + count:
            break
    got = np.concatenate(out) if out else np.zeros((0, 2), "<i2")
    if len(got) != count:
        raise ValueError(f"{Path(path).name}: wanted {count} frames from "
                         f"{start}, file ended after {pos}")
    return got


# --- fingerprints --------------------------------------------------------------

def _stereo(pcm) -> np.ndarray:
    if isinstance(pcm, (bytes, bytearray, memoryview)):
        pcm = np.frombuffer(pcm, "<i2")
    return np.ascontiguousarray(pcm, dtype="<i2").reshape(-1, 2)


def sha256_of(data) -> str:
    """sha256 of PCM (array or bytes) or of any bytes-like object."""
    if isinstance(data, np.ndarray):
        data = np.ascontiguousarray(data)
    return hashlib.sha256(data).hexdigest()


def crc32_of(pcm) -> str:
    return f"{zlib.crc32(_stereo(pcm)) & 0xffffffff:08X}"


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _window_sha(st: np.ndarray, pos: int) -> str:
    return hashlib.sha256(st[pos:pos + ANCHOR_FRAMES]).hexdigest()[:16]


def _blocks(st: np.ndarray) -> list[str]:
    return [f"{zlib.crc32(st[i:i + BLOCK_FRAMES]) & 0xffffffff:08X}"
            for i in range(0, len(st), BLOCK_FRAMES)]


def fingerprint(pcm) -> dict:
    """The pin for one input track (see the module docstring)."""
    st = _stereo(pcm)
    n = len(st)
    fp = {"frames": n, "sha256": sha256_of(st), "crc32": crc32_of(st),
          "anchors": [], "blocks": _blocks(st)}
    L = ANCHOR_FRAMES
    if n < 4 * L:
        return fp                      # too short to search for; sha only
    seen = set()
    for f in _ANCHOR_AT:
        centre, best = int(f * (n - L)), None
        for j in range(-8, 9):
            p = centre + j * L
            if p < 0 or p + L > n or p in seen:
                continue
            w = st[p:p + L].astype(np.int64)
            sl, sr = int(w[:, 0].sum()), int(w[:, 1].sum())
            if sl == 0 or sr == 0:
                continue
            energy = int(np.abs(w).sum())
            if best is None or energy > best[0]:
                best = (energy, p, sl, sr)
        if best:
            seen.add(best[1])
            fp["anchors"].append([best[1], best[2], best[3], _window_sha(st, best[1])])
    return fp


# --- checking ------------------------------------------------------------------

@dataclass
class TrackCheck:
    key: str
    pin: dict
    path: Path | None = None           # the file the track list points at
    frames: int | None = None
    crc32: str | None = None
    sha256: str | None = None
    status: str = "MISSING"            # MATCH | ALIGNED | DIFFERENT | MISSING
    notes: list[str] = field(default_factory=list)
    segments: list | None = None       # ALIGNED: [(path|None, start, count)]
    source_sha256: str | None = None   # the file's own bytes, for the audit

    @property
    def ok(self) -> bool:
        return self.status in ("MATCH", "ALIGNED")


def _frames_hint(have: int, want: int) -> str:
    d = have - want
    if d == 0:
        return ""
    more = "longer" if d > 0 else "shorter"
    if d % SECTOR_FRAMES == 0:
        return (f"{abs(d)} frames {more} than the verified rip -- exactly "
                f"{abs(d) // SECTOR_FRAMES} CD sector(s): gap/pregap handling "
                f"(e.g. gaps appended to the next track instead of the "
                f"previous one, or a pregap left out), or a different release")
    return (f"{abs(d)} frames {more} than the verified rip, not a whole number "
            f"of CD sectors: trimmed or padded (silence removal, a different "
            f"split), or not a straight CD rip")


def _mmss(frames: int) -> str:
    sec = frames / 44100
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def _block_diff(st: np.ndarray, pin: dict) -> str:
    mine, theirs = _blocks(st), pin.get("blocks", [])
    bad = [i for i, (a, b) in enumerate(zip(mine, theirs)) if a != b]
    n = len(theirs)
    if not bad:
        return "every 5-second block matches, yet the whole does not (please report this)"
    if set(bad) <= {0, n - 1}:
        which = ("the first and the last 5-second block differ" if len(bad) == 2
                 else "only the first 5 seconds differ" if bad == [0]
                 else "only the last 5-second block differs")
        return (f"{which}: the samples at the track's edge are not the verified "
                f"ones (a read offset or gap setting that differs for this "
                f"track alone, or an edge trimmed, padded or faded)")
    if bad == list(range(bad[0], n)):
        return (f"everything from {_mmss(bad[0] * BLOCK_FRAMES)} to the end "
                f"differs ({len(bad)} of {n} 5-second blocks): the track is cut "
                f"short or its ending differs")
    if bad == list(range(0, bad[-1] + 1)):
        return (f"everything up to {_mmss((bad[-1] + 1) * BLOCK_FRAMES)} differs "
                f"({len(bad)} of {n} 5-second blocks): the start of the track "
                f"is missing or differs")
    return (f"{len(bad)} of {n} 5-second blocks differ, the first at "
            f"{_mmss(bad[0] * BLOCK_FRAMES)}: an inexact read, a different "
            f"pressing or master, or processing (volume change, de-emphasis, "
            f"dither)")


def _cut(files: list[Path], frames: list[int], start: int, count: int) -> list:
    """Segments covering group-stream frames [start, start+count)."""
    segs, pos, total = [], 0, sum(frames)
    if start < 0:
        segs.append((None, 0, min(-start, count)))
    for path, n in zip(files, frames):
        lo, hi = max(start, pos), min(start + count, pos + n)
        if lo < hi:
            segs.append((path, lo - pos, hi - lo))
        pos += n
    tail = start + count - max(total, start)
    if tail > 0:
        segs.append((None, 0, min(tail, count)))
    return segs


def materialize(segments: list, reader: str) -> np.ndarray:
    """Interleaved int16 from segments (None = digital silence)."""
    parts = [read_range(p, reader, s, n) for p, s, n in segments]
    return np.concatenate(parts).reshape(-1).astype("<i2")


class _Scanner:
    """Finds anchor windows of many pins in one pass over a file group."""

    def __init__(self, checks: list[TrackCheck]):
        self.by_sum: dict[int, list] = {}
        for c in checks:
            for i, (pos, sl, sr, sha) in enumerate(c.pin.get("anchors", [])):
                self.by_sum.setdefault(sl, []).append((c.key, i, pos, sr, sha))
        self.keys = np.array(sorted(self.by_sum), dtype=np.int64)

    def scan(self, path: Path, reader: str, fidx: int, hits: list) -> int:
        L, carry, base = ANCHOR_FRAMES, np.zeros((0, 2), "<i2"), 0
        total = 0
        for chunk in iter_pcm(path, reader):
            total += len(chunk)
            buf = np.concatenate((carry, chunk)) if len(carry) else chunk
            if len(buf) >= L and len(self.keys):
                cl = np.concatenate(([0], np.cumsum(buf[:, 0], dtype=np.int64)))
                sums = cl[L:] - cl[:-L]
                for i in np.nonzero(np.isin(sums, self.keys))[0]:
                    i = int(i)
                    cands = self.by_sum[int(sums[i])]
                    sr = int(buf[i:i + L, 1].sum(dtype=np.int64))
                    for key, aidx, apos, want_sr, sha in cands:
                        if sr == want_sr and _window_sha(buf, i) == sha:
                            hits.append((key, aidx, apos, fidx, base + i))
            keep = min(L - 1, len(buf))
            carry = buf[len(buf) - keep:] if keep else buf[:0]
            base += len(buf) - keep
        return total


def check_tracks(pins: dict, want: dict[str, Path | None],
                 groups: list[list[Path]], reader: str,
                 describe=lambda p: p.name) -> dict[str, TrackCheck]:
    """Check each wanted pin key against the file the track list names, and
    search `groups` (contiguous file runs, e.g. one per disc) for any that do
    not match.  `describe` names a file in notes."""
    out: dict[str, TrackCheck] = {}
    for key, path in want.items():
        pin = pins["tracks"][key]
        c = out[key] = TrackCheck(key, pin, path)
        if path is None or not Path(path).exists():
            c.path = None
            continue
        pcm = _stereo(read_pcm(path, reader))
        c.frames, c.crc32, c.sha256 = len(pcm), crc32_of(pcm), sha256_of(pcm)
        c.status = "MATCH" if c.sha256 == pin["sha256"] else "DIFFERENT"
        del pcm

    _search(out, groups, reader, describe)
    for c in out.values():
        if c.status == "DIFFERENT":
            h = _frames_hint(c.frames, c.pin["frames"])
            if h:
                c.notes.insert(0, h)
    return out


def _search(out: dict[str, TrackCheck], groups, reader, describe) -> None:
    pending = {k: c for k, c in out.items() if not c.ok}
    searchable = {k: c for k, c in pending.items() if c.pin.get("anchors")}
    for k, c in pending.items():
        if k not in searchable:
            c.notes.append("too short to search the rip for; only an exact "
                           "match of the file itself is accepted")
    if not searchable:
        return

    # the groups holding a pending track's own file first; stop when all found
    order = sorted(range(len(groups)), key=lambda g: not any(
        c.path in groups[g] for c in searchable.values()))
    for g in order:
        if not searchable:
            break
        files = groups[g]
        scanner, hits, frames = _Scanner(list(searchable.values())), [], []
        for fidx, f in enumerate(files):
            frames.append(scanner.scan(f, reader, fidx, hits))
        offsets = np.concatenate(([0], np.cumsum(frames))).astype(np.int64)
        for key in list(searchable):
            c = searchable[key]
            mine = [h for h in hits if h[0] == key]
            if not mine:
                continue
            votes = Counter()
            for _, aidx, apos, fidx, pos in set(mine):
                votes[int(offsets[fidx]) + pos - apos] += 1
            expect = (int(offsets[files.index(c.path)])
                      if c.path in files else 0)
            start, n_votes = max(votes.items(),
                                 key=lambda kv: (kv[1], -abs(kv[0] - expect)))
            segs = _cut(files, frames, start, c.pin["frames"])
            pcm = _stereo(materialize(segs, reader))
            n_anchors = len(c.pin["anchors"])
            del searchable[key]
            where = _describe_segments(segs, describe)
            own = (c.path in files and
                   expect <= start < expect + frames[files.index(c.path)])
            if sha256_of(pcm) == c.pin["sha256"]:
                c.status, c.segments = "ALIGNED", segs
                if own:
                    shift = start - expect
                    c.notes.insert(0, (
                        f"your file holds this recording shifted by {shift:+d} "
                        f"samples ({shift / SECTOR_FRAMES:+.2f} CD sectors): a "
                        f"different read-offset correction or gap handling; "
                        f"re-cut exactly from {where}") if shift else
                        f"re-cut exactly from {where}")
                else:
                    c.notes.insert(0, f"found and re-cut exactly from {where} "
                                      f"(your tags/numbering put it elsewhere)")
            else:
                shift = start - expect
                at = (f"shifted by {shift:+d} samples ({shift / SECTOR_FRAMES:+.2f} "
                      f"CD sectors) in your file" if own and shift else where)
                c.notes.append(f"the same recording is there ({n_votes} of "
                               f"{n_anchors} anchors found, {at}), but it cannot be "
                               f"re-cut exactly: " + _block_diff(pcm, c.pin))
            del pcm
    for c in searchable.values():
        c.notes.append(("no file is numbered as this track, and " if c.path is None
                        else "") +
                       "the verified recording was not found anywhere in "
                       "these files: a different recording or master, or a "
                       "lossy, resampled or volume-changed copy")


def _describe_segments(segs, describe) -> str:
    parts = []
    for p, s, n in segs:
        parts.append(f"{n} frames of silence" if p is None
                     else f"{describe(p)} [{s}:{s + n}]")
    return " + ".join(parts)


def format_report(title: str, checks: dict[str, TrackCheck], pins_name: str,
                  describe=lambda p: p.name) -> str:
    rows = [f"[inputs] {title}: {len(checks)} input track(s) against {pins_name}",
            f"  {'track':<8} {'frames':>9}  {'CRC32':<8}  {'result':<9}  your file"]
    for key, c in checks.items():
        frames = f"{c.frames}" if c.frames is not None else "-"
        rows.append(f"  {key:<8} {frames:>9}  {c.crc32 or '-':<8}  "
                    f"{c.status:<9}  {describe(c.path) if c.path else '(none)'}")
        if c.status != "MATCH":
            rows.append(f"  {'':<8} {'verified:':>9}  {c.pin['crc32']:<8}  "
                        f"{c.pin['frames']} frames")
        for note in c.notes:
            rows.append(f"  {'':<8} -> {note}")
    counts = Counter(c.status for c in checks.values())
    rows.append("  " + ", ".join(f"{counts[s]} {s.lower()}" for s in
                                 ("MATCH", "ALIGNED", "DIFFERENT", "MISSING")
                                 if counts[s]))
    return "\n".join(rows)


def load_checked(c: TrackCheck, reader: str) -> np.ndarray:
    """The PCM a build uses for this input: the pinned samples when the
    check passed (re-verified), else the user's file as it stands."""
    if c.status == "ALIGNED":
        pcm = materialize(c.segments, reader)
        if sha256_of(pcm) != c.pin["sha256"]:
            raise RuntimeError(f"{c.key}: re-cut no longer matches its pin "
                               f"(the files changed during the build?)")
        return pcm
    if c.path is None:
        raise FileNotFoundError(f"{c.key}: no source file")
    return read_pcm(c.path, reader)


def load_pins(path: Path) -> dict | None:
    if not path.exists():
        return None
    pins = json.loads(path.read_text(encoding="utf-8"))
    if pins.get("format") != FORMAT:
        raise ValueError(f"{path.name}: unknown pin format {pins.get('format')!r}")
    return pins


# --- build audit -------------------------------------------------------------

def environment() -> dict:
    try:
        out = subprocess.run([ffmpeg_exe(), "-version"], capture_output=True,
                             timeout=60).stdout
        ff = out.decode(errors="replace").splitlines()[0].split(" Copyright")[0].strip()
    except (OSError, IndexError, subprocess.TimeoutExpired):
        ff = "unavailable"
    return {"ffmpeg": ff, "numpy": np.__version__,
            "python": platform.python_version(), "platform": platform.platform(),
            "machine": platform.machine(), "byteorder": sys.byteorder}


_STAGES = ("input", "pre_encode", "adx", "loop")


class Audit:
    """Per-stage record of one pack build, compared against its pins."""

    def __init__(self, pack: str, pins: dict | None, checks: dict[str, TrackCheck]):
        self.pack, self.pins, self.checks = pack, pins, checks
        self.tracks: list[dict] = []

    def track(self, name: str, inputs: list[str], pre_encode, adx: bytes,
              loop: tuple) -> dict:
        row = {"index": len(self.tracks), "name": name, "inputs": inputs,
               "pre_encode_sha256": sha256_of(pre_encode),
               "adx_sha256": sha256_of(adx), "loop": list(loop)}
        self.tracks.append(row)
        return row

    def pins_entry(self, cpk: Path) -> dict:
        """This build as the `packs` entry of a pins manifest."""
        return {"sha256": file_sha256(cpk),
                "tracks": [{k: t[k] for k in ("name", "inputs", "pre_encode_sha256",
                                              "adx_sha256", "loop")}
                           for t in self.tracks]}

    def diagnose(self, cpk: Path | None) -> tuple[str | None, str]:
        """(first differing stage or None, sentence)."""
        pp = (self.pins or {}).get("packs", {}).get(self.pack)
        if cpk is None:
            return "input", "not built: input tracks differ from the verified rip"
        if pp is None:
            return None, "no pins for this pack; only its final hash is checked"
        if file_sha256(cpk) == pp["sha256"]:
            return None, "identical to the verified build"
        if len(pp["tracks"]) != len(self.tracks):
            return "container", (f"the pack has {len(self.tracks)} tracks, the "
                                 f"verified build {len(pp['tracks'])}")
        first: dict[str, list[str]] = {}
        for mine, ref in zip(self.tracks, pp["tracks"]):
            label = mine["name"] if mine["inputs"] == [mine["name"]] else \
                f"{mine['name']} ({'+'.join(mine['inputs'])})"
            if any(k in self.checks and not self.checks[k].ok for k in mine["inputs"]):
                stage = "input"
            elif mine["pre_encode_sha256"] != ref["pre_encode_sha256"]:
                stage = "pre_encode"
            elif mine["adx_sha256"] != ref["adx_sha256"]:
                stage = "adx"
            elif mine["loop"] != ref["loop"]:
                stage = "loop"
            else:
                continue
            first.setdefault(stage, []).append(label)
        env, ref_env = environment(), (self.pins or {}).get("built_with", {})
        for stage in _STAGES:
            if stage not in first:
                continue
            names = first[stage]
            which = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
            if stage == "input":
                return stage, (f"first difference: the INPUT audio -- {len(names)} "
                               f"track(s) come from rip files that differ from the "
                               f"verified rip ({which}); see the input table above")
            if stage == "pre_encode":
                return stage, (f"first difference: the inputs match, but the PCM "
                               f"prepared for encoding (loop/fade processing) differs "
                               f"on {which}. That processing runs in numpy: this "
                               f"machine has numpy {env['numpy']} / Python "
                               f"{env['python']}, the verified build numpy "
                               f"{ref_env.get('numpy', '?')} / Python "
                               f"{ref_env.get('python', '?')}. Please report it with "
                               f"the .audit.json file")
            if stage == "adx":
                return stage, (f"first difference: the inputs and the PCM handed to "
                               f"the encoder match, but ffmpeg's ADX encoder produced "
                               f"different bytes on {which}. This machine: "
                               f"{env['ffmpeg']}; verified build: "
                               f"{ref_env.get('ffmpeg', '?')}")
            return stage, (f"first difference: the loop points on {which} (the "
                           f"audio matches). Please report it with the .audit.json file")
        return "container", ("every track's audio and loop points match the "
                             "verified build; the difference is in the pack "
                             "container. Please report it with the .audit.json file")

    def write(self, path: Path, cpk: Path | None, refused: str | None = None) -> dict:
        stage, sentence = self.diagnose(cpk)
        if refused:
            stage, sentence = "input", refused
        rec = {
            "pack": self.pack,
            "status": "refused" if refused else "built",
            "sha256": file_sha256(cpk) if cpk else None,
            "verified_sha256": (self.pins or {}).get("packs", {}).get(self.pack, {}).get("sha256"),
            "first_difference": stage,
            "diagnosis": sentence,
            "environment": environment(),
            "inputs": {k: {"status": c.status, "file": str(c.path) if c.path else None,
                           "file_sha256": c.source_sha256, "frames": c.frames,
                           "crc32": c.crc32, "pcm_sha256": c.sha256,
                           "verified_frames": c.pin["frames"],
                           "verified_crc32": c.pin["crc32"],
                           "verified_pcm_sha256": c.pin["sha256"], "notes": c.notes}
                       for k, c in self.checks.items()
                       if any(k in t["inputs"] for t in self.tracks) or refused},
            "tracks": self.tracks,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
        tmp.replace(path)
        return rec


def write_pins(path: Path, source: dict, tracks: dict[str, dict],
               packs: dict[str, dict]) -> None:
    """Write a pins manifest (maintainer step, from the verified sources)."""
    env = environment()
    doc = {"format": FORMAT, "source": source,
           "built_with": {k: env[k] for k in ("ffmpeg", "numpy", "python", "machine")},
           "anchor_frames": ANCHOR_FRAMES, "block_frames": BLOCK_FRAMES,
           "tracks": tracks, "packs": packs}
    path.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(f"[inputs] wrote {path.name}: {len(tracks)} tracks, {len(packs)} pack(s)")


def refusal(checks: dict[str, TrackCheck], allow: bool) -> str | None:
    """Why a build must not go ahead on these inputs, or None."""
    bad = [k for k, c in checks.items() if not c.ok]
    if not bad:
        return None
    absent = [k for k in bad if checks[k].path is None]
    if allow and not absent:
        print(f"[inputs] WARNING: building from {len(bad)} input track(s) that "
              f"differ from the verified rip ({', '.join(bad)}); the pack "
              f"will not match the published one")
        return None
    why = (f"not built: {len(bad)} of {len(checks)} input tracks differ from "
           f"the verified rip ({', '.join(bad)}) -- see the table above")
    if absent:
        return why + "; tracks with no source at all cannot be built from"
    return why + ("; --allow-input-mismatch (make_packs.py --no-verify) builds "
                  "it anyway, but it will not match the published pack")


def gate(pack: str, checks: dict[str, TrackCheck], pins: dict | None,
         cpk: Path, allow: bool, check_only: bool, tag: str) -> None:
    """The shared end of an input check.  --check-inputs exits with its
    verdict (0 all match, 3 not); inputs that differ stop the build with
    exit status 3 and a refused audit record, unless allowed."""
    if check_only:
        raise SystemExit(0 if all(c.ok for c in checks.values()) else 3)
    why = refusal(checks, allow)
    if why is None:
        return
    print(f"{tag} {pack}: {why}")
    cpk.unlink(missing_ok=True)
    Audit(pack, pins, checks).write(audit_path(cpk), None, refused=why)
    raise SystemExit(3)


def audit_path(cpk: Path) -> Path:
    return cpk.with_name(cpk.stem + ".audit.json")
