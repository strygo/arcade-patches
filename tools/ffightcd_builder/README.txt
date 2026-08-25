Final Fight CD — cutscene backport reconstruction kit
=====================================================

Version:      rc1 (2026-08-25)
Hardware:     Capcom CPS-1
Target:       HBMAME sets ffightus01 / ffightjs01, and MiSTer (jtcps1)
Backport by:  Steve Gordon (https://x.com/strygo)
Website:      https://strygo.github.io/arcade-patches/final-fight-cd/

This kit rebuilds the CPS-1 arcade backport of Final Fight's Sega CD
cutscenes from your own disc images and your own arcade romset. No game
data is included.

The cutscenes are not captured from an emulator. A 68000 interpreter runs
the disc's own scene script and renders the Mega Drive video state it
produces, so the frames are the game's output rather than a recording of
it — then they are converted to CPS-1 tiles and built into the ROM.


WHAT YOU NEED
-------------

  1. Your Final Fight CD disc image, for the region you're building:
       - USA   : "Final Fight CD (USA)" track 1 .bin  -> ffightus01
       - Japan : "Final Fight CD (JP).img" (or the .bin of track 1)
                                                      -> ffightjs01
     Supply both to build both sets.

  2. Your arcade Final Fight romset, MAME 0.260-era, in one directory:
       ffight.7z (World), ffightu.7z (USA), ffightj.7z (Japan)

  3. Python 3.9 or newer with numpy and Pillow, and 7zz (or 7z) on PATH.
     No emulator, no assembler.


USAGE
-----

    python3 apply.py --disc-us "Final Fight CD (USA) (Track 01).bin" \
                     --romset /path/to/roms --out-dir out

Add --disc-jp "Final Fight CD (JP).img" to build the Japan set as well;
the build covers whichever regions you supplied a disc for. This writes:

    out/hbmame/ffightus01.zip        for HBMAME
    out/hbmame/ffightjs01.zip
    out/mister/_Arcade/_Backports/*.mra   MiSTer, laid out like the SD
    out/mister/games/hbmame/*.zip    you can copy out/mister/'s contents
                                     to the card root

The first build takes about six minutes for both regions, less for one;
each stage prints as it goes.

A successful build removes its own scratch directory, so it leaves you the
sets and nothing else. Pass --cache to keep it if you want to look at the
rendered scenes; a re-run renders them again either way.



ON MISTER
---------

Copy the contents of out/mister/ to the root of the MiSTer SD card:

    _Arcade/_Backports/Final Fight (CD Cutscenes).mra
    games/hbmame/ffightus01.zip

The standard Jotego jtcps1 core is required.

These MRAs carry the cutscenes only, with the game's own arcade audio. If
you want the Sega CD soundtrack and voices alongside them, that is the
separate CPS+ release, which ships its own combined MRA.


WHAT IS AND IS NOT IN THIS KIT
------------------------------

Included: our reconstruction code, our own measurements (scene tables,
caption layouts, voice timings), the mouth data, and a list of 24 byte
patches that retime the arcade story sequence.

Reconstructed on your machine from your files: the cutscene graphics,
palettes and script; the stock graphics and sound; and the retimed
program ROMs, which are your own ROMs with those 24 patches applied.

The kit writes only into work/ while it runs, and clears it when the
build succeeds.


LEGAL
-----

Unofficial fan reconstruction. Not affiliated with or endorsed by Capcom.
All game titles, characters and artwork remain the property of their
owners. You must own the discs and the romset used as inputs. Do not sell
this kit or distribute it applied to game images.
