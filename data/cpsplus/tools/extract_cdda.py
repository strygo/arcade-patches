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

A PREGAP command describes pregap sectors the image does NOT contain (the
drive generates them as silence), so with --pregap keep that many sectors
of digital silence are inserted before the track: the result is what a rip
that stored the pregap holds.  With --pregap trim it changes nothing.  None
of the verified rips has a PREGAP line, so their output is unaffected.

Cue sheets are read as UTF-8, then Shift-JIS (cp932), then Windows-1252 --
whichever names image files that exist -- and FILE names may be quoted or
not, use either slash, and differ in letter case from the file on disk.

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
import struct
import wave
from pathlib import Path, PureWindowsPath

SECTOR = 2352
FPS = 75  # CD frames per second
MDS_NEXT_PREGAP_SECTORS = 150   # 2.00 s, see module docstring


def msf_to_sector(msf: str) -> int:
    m, s, f = (int(x) for x in msf.split(":"))
    return (m * 60 + s) * FPS + f


CUE_ENCODINGS = ("utf-8-sig", "cp932", "cp1252")


def _file_name(line: str) -> str:
    """The name in `FILE "name" TYPE` or an unquoted `FILE name TYPE`."""
    rest = line.strip()[4:].strip()
    if rest.startswith('"'):
        end = rest.find('"', 1)
        if end > 0:
            return rest[1:end]
    parts = rest.rsplit(None, 1)          # the last word is the file type
    return parts[0].strip('"') if len(parts) == 2 else rest.strip('"')


def _resolve(cue_dir: Path, name: str) -> Path:
    """The image file a cue names: as written, with Windows separators, by
    bare name beside the cue, or ignoring letter case."""
    for cand in (cue_dir / name, cue_dir.joinpath(*PureWindowsPath(name).parts),
                 cue_dir / PureWindowsPath(name).name):
        try:
            if cand.is_file():
                return cand
        except OSError:
            pass
    low = PureWindowsPath(name).name.lower()
    for f in cue_dir.iterdir():
        if f.name.lower() == low:
            return f
    return cue_dir / name


def _cue_lines(cue_path: Path) -> list[str]:
    raw = cue_path.read_bytes()
    decoded = []
    for enc in CUE_ENCODINGS:
        try:
            decoded.append(raw.decode(enc).splitlines())
        except UnicodeDecodeError:
            continue
    if not decoded:
        return raw.decode("latin-1").splitlines()
    for lines in decoded:                 # the reading whose files exist
        names = [_file_name(t) for t in lines if t.strip().upper().startswith("FILE")]
        if names and all(_resolve(cue_path.parent, n).is_file() for n in names):
            return lines
    return decoded[0]


def cue_files(cue_path: Path) -> list[Path]:
    """Every image file the cue names, resolved beside it."""
    return [_resolve(cue_path.parent, _file_name(t)) for t in _cue_lines(cue_path)
            if t.strip().upper().startswith("FILE")]


def parse_cue(cue_path: Path):
    """Returns [(n, mode, file_path, i0_sector|None, i1_sector, pregap_sectors)],
    plus a flag telling whether the cue is one-file-per-track (redump) or
    single-image.  INDEX sectors are relative to the track's own FILE."""
    entries = []
    files = set()
    cur_file = None
    cur = None  # [n, mode, file, i0, i1, pregap]
    for line in _cue_lines(cue_path):
        t = line.strip()
        word = t.split(None, 1)[0].upper() if t else ""
        if word == "FILE":
            cur_file = _resolve(cue_path.parent, _file_name(t))
            files.add(cur_file)
        elif word == "TRACK":
            parts = t.split()
            if cur:
                entries.append(tuple(cur))
            cur = [int(parts[1]), parts[2], cur_file, None, None, 0]
        elif word == "INDEX" and cur is not None:
            parts = t.split()
            if int(parts[1]) == 0:
                cur[3] = msf_to_sector(parts[2])
            elif int(parts[1]) == 1:
                cur[4] = msf_to_sector(parts[2])
            # INDEX 02+ are subindexes inside the track, not boundaries
        elif word == "PREGAP" and cur is not None:
            cur[5] = msf_to_sector(t.split()[1])
        elif word == "POSTGAP" and cur is not None:
            print(f"note: {cue_path.name} track {cur[0]} has a POSTGAP; its "
                  f"generated silence is not added")
    if cur:
        entries.append(tuple(cur))
    for e in entries:
        if e[2] is None or not e[2].is_file():
            raise FileNotFoundError(
                f"{cue_path.name}: track {e[0]} names an image file that is "
                f"not beside the cue ({e[2].name if e[2] else 'no FILE line'})")
        if e[4] is None:
            raise ValueError(f"{cue_path.name}: track {e[0]} has no INDEX 01")
    return entries, len(files) > 1


def track_pcm(entries, multi_file, n, pregap="trim") -> bytes:
    by_n = {e[0]: e for e in entries}
    num, mode, fpath, i0, i1, pregap_sectors = by_n[n]
    if not mode.upper().startswith("AUDIO"):
        raise SystemExit(f"track {n} is {mode}, not AUDIO")
    start = i1 if pregap == "trim" else (i0 if i0 is not None else i1)
    # The track runs to the next track's first index IN THE SAME FILE, else
    # to the end of its file (one file per track: the file verbatim).
    nxt = [e for e in entries if e[0] > n and e[2] == fpath]
    size_sectors = fpath.stat().st_size // SECTOR
    end = (nxt[0][3] if nxt[0][3] is not None else nxt[0][4]) if nxt else size_sectors
    with open(fpath, "rb") as fh:
        fh.seek(start * SECTOR)
        # a one-file-per-track image is taken to EOF verbatim; a shared
        # image is cut at whole sectors
        data = fh.read() if (multi_file and not nxt) else fh.read((end - start) * SECTOR)
    if pregap == "keep" and pregap_sectors:
        # sectors the image does not store: the drive plays them as silence
        data = bytes(pregap_sectors * SECTOR) + data
    return data


def write_wav(path: Path, pcm: bytes) -> None:
    """16-bit stereo 44.1 kHz WAV, the extractor's one output format."""
    w = wave.open(str(path), "wb")
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
    w.writeframes(pcm)
    w.close()


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
        write_wav(out, pcm)
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
