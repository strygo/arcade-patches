Street Fighter Alpha 2 Gold / Zero 2 Dash — reconstruction kit
==============================================================

Version:      rc3 (2026-09-17)
Hardware:     Capcom CPS-2
Target:       MAME and HBMAME sets, and MiSTer (jtcps2) — 4 regions
Backport by:  Steve Gordon (https://x.com/strygo)
Website:      https://strygo.github.io/arcade-patches/sfa2-gold/

This kit builds the CPS-2 arcade backport of Street Fighter Alpha 2 Gold /
Zero 2 Dash from your own PlayStation 2 disc image and arcade romset. Original
game ROMs are not included.

WHAT YOU NEED
-------------

  1. Your PlayStation 2 anthology disc image:
       - USA / Asia : "Street Fighter Alpha Anthology" (USA)
       - Japan      : "Street Fighter Zero - Fighter's Generation" (Japan)
       - Europe     : "Street Fighter Alpha Anthology" (Europe)
  2. Your arcade Street Fighter Zero 2 Alpha romset:
       - sfz2al.zip  for the USA / Europe / Asia sets
       - sfz2alj.zip for the Japan set
  3. Python 3.8 or newer. Nothing else — no emulator, no assembler.

USAGE (recommended — build every platform for one region)
----------------------------------------------------------

    python3 apply.py --iso <anthology.iso> --romset <sfz2al.zip> \
                     --region <jp|us|eu|asia> --out-dir out

This creates:

    out/mame/<region>/<set>.zip  4MB — stock MAME or real CPS-2 hardware
    out/hbmame/<set>.zip         8MB — HBMAME with expanded Cammy audio
    out/mister/                  8MB — ready to copy to a MiSTer SD card

Use the same output folder when building more than one region. Each region
needs the matching disc listed above.

REGIONS
-------

    us    Street Fighter Alpha 2 Gold
    eu    Street Fighter Alpha 2 Dash
    jp    Street Fighter Zero 2 Dash (Japan)
    asia  Street Fighter Zero 2 Dash (English)

USAGE (single file)
-------------------

    python3 apply.py --iso ... --romset ... --region us --size 8mb --out sfa2g.zip

The kit verifies the finished files automatically. It stops with an error if
the supplied disc or romset does not match the selected region.

MAME and HBMAME will show checksum warnings because this is an unofficial
build. Those warnings are expected.

ON MISTER
---------

Copy the contents of out/mister/ to the root of the MiSTer SD card:

    _Arcade/_Arcade Patches/_Enhanced Versions/<name>.mra          the USA MRA
    _Arcade/_Arcade Patches/_Enhanced Versions/_Europe/<name>.mra  the other
    _Arcade/_Arcade Patches/_Enhanced Versions/_Asia/<name>.mra    regions, each
    _Arcade/_Arcade Patches/_Enhanced Versions/_Japan/<name>.mra   in its folder
    games/hbmame/<set>.zip                                         the 8MB sets

Earlier releases put these MRAs in _Arcade/_Backports; delete them from there
when you copy the new ones over.

The standard Jotego jtCPS2 core is required. No separate qsound.zip is needed.

LEGAL
-----

Unofficial fan reconstruction. Not affiliated with or endorsed by Capcom. All
game titles, characters, and artwork remain the property of their owners. You
must own the disc and romset used as inputs. Do not sell this kit or distribute
it applied to game images.
