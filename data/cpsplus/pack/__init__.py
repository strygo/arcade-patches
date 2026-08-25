"""cpsplus.pack — CPS2 arranged-audio pack toolchain (Phase 1).

Builds, verifies, and auditions `.cpk` audio packs per PACK_FORMAT.md
(binary layout v0).  Entry point: ../build_pack.py.

Modules:
  format    — frozen binary layout v0: writer + reader
  adxcodec  — CRI ADX header parse/synthesis, coefficients, ffmpeg bridge
  isofs     — ISO9660 walker for 2048 B (PS2 DVD) and 2352 B (Saturn CD) sectors
  afs       — CRI AFS archive walker (region inside a larger file)
  mus       — Saturn MUS stream decoder (s16BE stereo 32000 Hz, blk4096)
  sources   — input resolution (zip member extraction with caching)
  protocols — per-game protocol descriptors + verified command maps
  build_hsf2, build_sfa2_arrange — automatic builder modes
  authoring — `init` / `build` pack-project (pack.toml) modes
  audition  — audition-to-WAV + structural/seam verification
  selftest  — synthetic round-trip self test

Shared probe logic promoted from ../tools/{iso9660,isolist,afs,adxscan,mus2wav}.py
(kept there as standalone probes; the package versions are the importable ones).
"""
