#!/usr/bin/env python3
"""Extract CD-DA audio tracks from a cue sheet rip to WAV files.

Deterministic and dumb on purpose: raw 2352-byte audio sectors are 16-bit LE
stereo 44.1 kHz PCM already.  Two cue layouts are supported:

  single-image (cue + one img/bin): each TRACK's audio spans
    [its INDEX 01, the next track's INDEX 00 or 01, or EOF)
  redump multi-bin (one FILE per TRACK): each TRACK's audio spans
    [its INDEX 01 within its own file, end of that file)

Pregap handling is an explicit, per-disc convention (both are raw disc
bytes; nothing is faded, resampled or otherwise modified):
  --pregap trim  (default)  audio starts at INDEX 01 -- matches the
                            Final Fight CD rips
  --pregap keep             each track is taken whole, own INDEX 00
                            pregap included -- matches the PCE CD rips
                            (multi-bin: the file verbatim)

Alcohol 120% rips (.mds + .mdf) are also supported: pass the .mds.  Sectors
are sector_size bytes (2448 = 2352 PCM + 96 subchannel; only the PCM is
copied).  Track audio = [start_offset, next track's start_offset or EOF)
minus 150 trailing sectors -- the next track's 2.00 s pregap.  That boundary
is empirical, not assumed: on the Muscle Bomber disc every track carried
137-148 sectors of trailing digital silence before the cut and 0.0 after
(internal disc-inventory notes), and the result is
byte-identical to the pack's shipped source WAVs on all 29 tracks.

Usage:
  extract_cdda.py DISC.cue OUTDIR [--tracks 24,25] [--pregap keep]
  extract_cdda.py DISC.mds OUTDIR
"""
import argparse
import re
import struct
import wave
from pathlib import Path

SECTOR = 2352
FPS = 75  # CD frames per second
MDS_NEXT_PREGAP_SECTORS = 150   # 2.00 s, see module docstring


def msf_to_sector(msf: str) -> int:
    m, s, f = (int(x) for x in msf.split(":"))
    return (m * 60 + s) * FPS + f


def parse_cue(cue_path: Path):
    """Returns [(n, mode, file_path, i0_sector|None, i1_sector)], plus a flag
    telling whether the cue is one-file-per-track (redump) or single-image."""
    entries = []
    files = set()
    cur_file = None
    cur = None  # [n, mode, file, i0, i1]
    for line in cue_path.read_text(errors="replace").splitlines():
        t = line.strip()
        if t.upper().startswith("FILE"):
            cur_file = cue_path.parent / re.findall(r'"([^"]+)"', t)[0]
            files.add(cur_file)
        elif t.upper().startswith("TRACK"):
            parts = t.split()
            if cur:
                entries.append(tuple(cur))
            cur = [int(parts[1]), parts[2], cur_file, None, None]
        elif t.upper().startswith("INDEX") and cur is not None:
            parts = t.split()
            cur[3 if parts[1] == "00" else 4] = msf_to_sector(parts[2])
    if cur:
        entries.append(tuple(cur))
    return entries, len(files) > 1


def track_pcm(entries, multi_file, n, pregap="trim") -> bytes:
    by_n = {e[0]: e for e in entries}
    num, mode, fpath, i0, i1 = by_n[n]
    if not mode.upper().startswith("AUDIO"):
        raise SystemExit(f"track {n} is {mode}, not AUDIO")
    data = fpath.read_bytes()
    start = i1 if pregap == "trim" else (i0 if i0 is not None else i1)
    if multi_file:
        # sectors are relative to this track's own file; audio runs to EOF
        return data[start * SECTOR:]
    total_sectors = len(data) // SECTOR
    nxt = [e for e in entries if e[0] > n]
    if nxt:
        end = nxt[0][3] if nxt[0][3] is not None else nxt[0][4]
    else:
        end = total_sectors
    return data[start * SECTOR:end * SECTOR]


def parse_mds(mds_path: Path):
    """Alcohol descriptor -> [(track_no, is_audio, byte_offset, sector_size)].
    Track blocks are 80 B; point >= 0xA0 are lead-in entries.  NOTE: the
    start_sector field is the disc TOC LBA (+150 lead-in) -- the FILE
    position is the start_offset byte field, which is what we use."""
    mds = mds_path.read_bytes()
    if mds[:16] != b"MEDIA DESCRIPTOR":
        raise SystemExit(f"{mds_path}: not an Alcohol 120% .mds")
    sess_off = struct.unpack_from("<I", mds, 0x50)[0]
    n_blocks = mds[sess_off + 10]
    tb_off = struct.unpack_from("<I", mds, sess_off + 20)[0]
    out = []
    for i in range(n_blocks):
        b = mds[tb_off + i * 80: tb_off + (i + 1) * 80]
        if b[4] >= 0xA0:
            continue
        sector_size = struct.unpack_from("<H", b, 0x10)[0]
        start_off = struct.unpack_from("<Q", b, 0x28)[0]
        out.append((b[4], b[0] == 0xA9, start_off, sector_size))
    return sorted(out)


def extract_mds(mds_path: Path, outdir: Path, tracks=None,
                quiet=False) -> list[Path]:
    entries = parse_mds(mds_path)
    mdf = mds_path.with_suffix(".mdf")
    if not mdf.exists():
        raise SystemExit(f"{mdf} missing (must sit beside the .mds)")
    data = mdf.read_bytes()
    want = tracks if tracks else [n for n, au, _, _ in entries if au]
    outdir.mkdir(parents=True, exist_ok=True)
    outs = []
    by_n = {e[0]: e for e in entries}
    order = [e[0] for e in entries]
    for n in want:
        num, is_audio, start, ssize = by_n[n]
        if not is_audio:
            raise SystemExit(f"track {n} is not audio")
        nxt = [m for m in order if m > n]
        end = by_n[nxt[0]][2] if nxt else len(data)
        end -= MDS_NEXT_PREGAP_SECTORS * ssize
        pcm = b"".join(data[o:o + SECTOR] for o in range(start, end, ssize))
        out = outdir / f"tr{n:02d}.wav"
        w = wave.open(str(out), "wb")
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(pcm)
        w.close()
        outs.append(out)
        if not quiet:
            print(f"  tr{n:02d}: {len(pcm)//4/44100:7.1f}s -> {out}")
    return outs


def extract(cue: Path, outdir: Path, tracks=None, quiet=False,
            pregap="trim") -> list[Path]:
    assert pregap in ("trim", "keep"), pregap
    if Path(cue).suffix.lower() == ".mds":
        if pregap != "trim":
            raise SystemExit("--pregap keep is not defined for .mds rips")
        return extract_mds(Path(cue), outdir, tracks, quiet)
    entries, multi_file = parse_cue(cue)
    want = (tracks if tracks
            else [e[0] for e in entries if e[1].upper().startswith("AUDIO")])
    outdir.mkdir(parents=True, exist_ok=True)
    outs = []
    for n in want:
        pcm = track_pcm(entries, multi_file, n, pregap)
        out = outdir / f"tr{n:02d}.wav"
        w = wave.open(str(out), "wb")
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(pcm)
        w.close()
        outs.append(out)
        if not quiet:
            print(f"  tr{n:02d}: {len(pcm)//4/44100:7.1f}s -> {out}")
    return outs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cue", type=Path, help="cue sheet or Alcohol .mds")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--tracks", help="comma list; default = every AUDIO track")
    ap.add_argument("--pregap", choices=("trim", "keep"), default="trim")
    a = ap.parse_args(argv)
    want = [int(x) for x in a.tracks.split(",")] if a.tracks else None
    extract(a.cue, a.outdir, want, pregap=a.pregap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
