#!/usr/bin/env python3
"""CPS2 arranged-audio pack builder (Phase 1 toolchain).

Builds `.cpk` packs per PACK_FORMAT.md §Binary layout v0, from:
  * hsf2        — Hyper SF2 AE PS2 disc (zip or iso), fully automatic
  * sfa2-arrange  — Saturn MUS-engine disc(s) (SFZ2 family), ADX-encoded
  * init/build  — community authoring from a pack.toml project

Plus: audition (pack -> WAV loop-joint listening check), verify
(structural + loop-seam + DDR-budget), selftest.

Examples:
  build_pack.py hsf2 --iso "roms/ps2/Hyper Street Fighter II - The
      Anniversary Edition (Japan).zip"
  build_pack.py hsf2 --iso ... --bank cps1
  build_pack.py sfa1-arrange --iso "roms/ps2/Street Fighter Alpha Anthology.iso"
  build_pack.py sfa2-arrange --iso "roms/saturn/Street Fighter Zero 2'
      (Japan).zip"
  build_pack.py audition work/packs/hsf2_arrange.cpk --cmd 0x01 --loops 2
  build_pack.py verify work/packs/hsf2_arrange.cpk
  build_pack.py init ssf2t && build_pack.py build ssf2t_pack/
  build_pack.py selftest

See pack/README.md for the full manual.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pack import audition as _audition          # noqa: E402
from pack import authoring, selftest            # noqa: E402
from pack import build_hsf2, build_sfa2_arrange  # noqa: E402
from pack import build_sfa1_arrange                    # noqa: E402
from pack import build_ssf2_arrange                     # noqa: E402
from pack import build_sf2_arrange                          # noqa: E402


# pass-through subcommands: one uniform entry point per pack; each delegates
# its remaining argv to the builder module's own CLI (see pack/README.md)
PASSTHROUGH = {
    "strider-psx": ("build_strider_psx", "Strider PSX Sound Remix recordings"),
    "remix": ("build_remix", "Double Impact and OC ReMix HD Remix album packs"),
    "ffight":     ("build_ffight_arrange",     "Final Fight arrange (Sega CD US+JP discs)"),
    "ffightae-cps2": ("build_ffightae_cps2_arrange", "Final Fight 30th Anniversary CPS2 Edition arrange (Sega CD US disc)"),
    "ffight-ost": ("build_ffight_ost", "Final Fight OST editions (snes/x68k)"),
    "sfa2-snes":  ("build_sfa2_snes",  "SFA2 with the SNES SFZ2 soundtrack"),
    "spf2t":      ("build_spf2t_arrange",      "Super Puzzle Fighter II Turbo arrange (Saturn)"),
    "mtwins":     ("build_mtwins_arrange",     "Mega Twins arrange (PCE CD)"),
    "forgottn":   ("build_forgottn_arrange",   "Forgotten Worlds arrange (PCE CD)"),
    "mbomber":    ("build_mbomber_arrange",    "Muscle Bomber arrange (FM Towns)"),
    "unsquad-snes": ("build_unsquad_snes",     "UN Squadron / Area 88 with the SNES soundtrack (CMG album)"),
    "ghouls-x68k-midi": ("build_ghouls_x68k_midi", "Ghouls'n Ghosts with the X68000 MIDI soundtrack"),
    "sf2ce-x68k-midi": ("build_sf2ce_x68k_midi", "SF2CE with the X68000 MIDI soundtrack"),
    "ssf2-x68k-midi": ("build_ssf2_x68k_midi", "SSF2 with the X68000 MIDI soundtrack"),
}


def main(argv=None):
    args_in = sys.argv[1:] if argv is None else list(argv)
    if args_in and args_in[0] in PASSTHROUGH:
        import importlib
        mod = importlib.import_module(f"pack.{PASSTHROUGH[args_in[0]][0]}")
        return mod.main(args_in[1:])
    ap = argparse.ArgumentParser(
        prog="build_pack.py",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("hsf2", help="build a pack from the HSF2 AE PS2 disc")
    p.add_argument("--iso", required=True, help="disc zip or extracted iso")
    p.add_argument("--bank", default="arrange",
                   choices=["arrange", "cps2", "cps1"])
    p.add_argument("--out")
    p.add_argument("--table-offset", type=lambda s: int(s, 0),
                   help="dispatch-table ELF file offset (default JP 0x63a0f0)")
    p.add_argument("--no-crosscheck", action="store_true")

    p = sub.add_parser("sfa1-arrange",
                       help="build the SFA1 ARRANGE pack -- everything from "
                            "the ONE Saturn SF Alpha disc (stages with the "
                            "carried-over authored loops + endings/utilities/"
                            "credit rolls)")
    p.add_argument("--disc", required=True, help="Saturn SF Alpha: Warriors' "
                   "Dreams rip (cue, or zip/7z containing one)")
    p.add_argument("--loops", help="Saturn-coordinate loop TSV (default "
                   "manifests/sfa1_arrange_loops_saturn.tsv)")
    p.add_argument("--trigger-map", help="trigger map TSV (default "
                   "manifests/sfa1_arrange_trigger_map.tsv)")
    p.add_argument("--out")

    p = sub.add_parser("ssf2-arrange",
                       help="build the SSF2/SSF2T ARRANGE pack (HSF2 AE arrange "
                            "ADX re-keyed to the standalone arcade commands)")
    p.add_argument("--game", required=True, choices=["ssf2", "ssf2t"])
    p.add_argument("--iso", required=True, help="HSF2 AE disc zip or iso")
    p.add_argument("--map", help="trigger map TSV "
                   "(default manifests/<game>_arrange_trigger_map.tsv)")
    p.add_argument("--out")
    p.add_argument("--no-crosscheck", action="store_true")

    p = sub.add_parser("sf2-arrange",
                       help="build the SF2 World Warrior (CPS1) ARRANGE pack "
                            "(HSF2 AE arrange ADX re-keyed to the sf2 byte-latch "
                            "stage commands)")
    p.add_argument("--iso", required=True, help="HSF2 AE disc zip or iso")
    p.add_argument("--map", help="trigger map TSV "
                   "(default manifests/sf2_arrange_trigger_map.tsv)")
    p.add_argument("--out")
    p.add_argument("--emit-map", action="store_true",
                   help="(re)write the tracked trigger map and exit")
    p.add_argument("--no-crosscheck", action="store_true")

    p = sub.add_parser("sfa2-arrange",
                       help="build from Saturn MUS disc(s) (SFZ2 family)")
    p.add_argument("--iso", required=True,
                   help="Saturn zip or Track-1 bin")
    p.add_argument("--game", default="sfz2al")
    p.add_argument("--trigger-map",
                   help="explicit cmd->song TSV (overrides the tracked "
                        "manifests/<game>_arrange_trigger_map.tsv)")
    p.add_argument("--out")
    p.add_argument("--no-crosscheck", action="store_true")

    for cmd_name, (_mod, help_text) in PASSTHROUGH.items():
        sub.add_parser(cmd_name, help=help_text, add_help=False)

    p = sub.add_parser("init", help="scaffold a community pack project")
    p.add_argument("game")
    p.add_argument("dir", nargs="?")

    p = sub.add_parser("build", help="build a pack.toml project")
    p.add_argument("dir")
    p.add_argument("--out")

    p = sub.add_parser("audition",
                       help="render a track (intro + N loops) to WAV")
    p.add_argument("pack")
    p.add_argument("--track", type=int)
    p.add_argument("--cmd", type=lambda s: int(s, 0))
    p.add_argument("--loops", type=int, default=2)
    p.add_argument("--out", help="output WAV file or directory")

    p = sub.add_parser("verify", help="structural + seam + budget checks")
    p.add_argument("pack")
    p.add_argument("--quick", action="store_true",
                   help="skip the decode-based loop-seam metrics")

    sub.add_parser("selftest", help="writer/reader round-trip self test")

    a = ap.parse_args(args_in)
    if a.command == "hsf2":
        build_hsf2.build(a.iso, out=a.out, bank=a.bank,
                         table_offset=a.table_offset,
                         crosscheck=not a.no_crosscheck)
    elif a.command == "sfa1-arrange":
        build_sfa1_arrange.build(a.disc, out=a.out, loops_path=a.loops,
                                 trigger_map=a.trigger_map)
    elif a.command == "ssf2-arrange":
        build_ssf2_arrange.build(a.game, a.iso, map_path=a.map, out=a.out,
                                 crosscheck=not a.no_crosscheck)
    elif a.command == "sf2-arrange":
        if a.emit_map:
            from pathlib import Path as _P
            build_sf2_arrange.emit_map(_P(a.map) if a.map
                                    else build_sf2_arrange.DEFAULT_MAP)
        else:
            build_sf2_arrange.build(a.iso, map_path=a.map, out=a.out,
                                 crosscheck=not a.no_crosscheck)
    elif a.command == "sfa2-arrange":
        build_sfa2_arrange.build(a.iso, out=a.out, game=a.game,
                           trigger_map=a.trigger_map,
                           crosscheck=not a.no_crosscheck)
    elif a.command == "init":
        authoring.init(a.game, a.dir)
    elif a.command == "build":
        authoring.build(a.dir, out=a.out)
    elif a.command == "audition":
        _audition.audition(a.pack, track=a.track, cmd=a.cmd, loops=a.loops,
                           out=a.out)
    elif a.command == "verify":
        sys.exit(0 if _audition.verify(a.pack, quick=a.quick) else 1)
    elif a.command == "selftest":
        sys.exit(0 if selftest.run() else 1)


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError) as e:
        # input-shaped problems: say what is wrong, skip the traceback
        raise SystemExit(f"error: {e}")
