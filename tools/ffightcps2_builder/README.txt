{TITLE} — reconstruction kit
{RULE}

Version:      (stamped at release)
Hardware:     Capcom CPS-2 (on MiSTer, with the CPS+ extensions)
Target:       MiSTer (CPS+ core, included), HBMAME and Darksoft —
              English restoration, USA and Japan
Backport by:  Steve Gordon (https://x.com/strygo)
Website:      https://arcadepatches.com/{SLUG}/

This kit builds Final Fight EX as a CPS-2 game, with the Sega CD version's
animated opening and ending, the EX title and options, and on MiSTer the
Sega CD's arranged soundtrack with voiced cutscenes, from your own Final
Fight CD discs and arcade romsets. No game data is included.


WHAT YOU NEED
-------------

  1. Your Final Fight CD disc, for the builds you want, as its .cue sheet
     (with its .bin or .img files beside it), a CD .chd, or a .zip or .7z
     holding one:
       - USA   : Final Fight CD (USA)   -> the USA build
       - Japan : Final Fight CD (JP)    -> the Japan build and the English
                                           restoration
     Supply both to build all three. The MiSTer music packs are made from
     the disc's CD audio, so give the whole disc, not only its first track.

  2. Your arcade romsets, MAME 0.260-era, as .zip or .7z, split or merged:
       - Final Fight: ffight (World), ffightu (USA) and ffightj (Japan)
       - Street Fighter Alpha 3: sfa3u (USA), for the CPS-2 sound

  3. Python 3.11 or newer with numpy and Pillow, with FFmpeg fetched on demand for the
     MiSTer music packs. .7z romsets and discs also need 7-Zip (7zz, 7z,
     or 7-Zip's normal Windows install); a .chd disc needs MAME's chdman.
     No emulator, no assembler.


USAGE
-----

(On Windows, run it as "py apply.py ..." on one line, or end each line with ^
in Command Prompt or ` in PowerShell instead of \.)

    python3 apply.py --disc-jp "Final Fight CD (JP).cue" \
                     --disc-us "Final Fight CD (USA).cue" \
                     --rompath <romset folder> --out-dir out

This builds every set your discs allow and writes:

    out/hbmame/<set>.zip         for HBMAME
    out/hbmame/definitions/      the sets' HBMAME definitions
    out/mister/                  laid out like the MiSTer SD card
    out/darksoft/games/          one game folder per build, for Darksoft

Add --platform mister, hbmame or darksoft to build for one target only. The
kit verifies every finished file automatically. It stops with an error if a
disc or a romset does not match.


SETS
----

    English restoration   ffex2r   Final Fight EX CPS2 (English Restoration)
    USA                   ffex2u   Final Fight EX CPS2 (USA)
    Japan                 ffex2j   Final Fight EX CPS2 (Japan)

The English restoration is the Japanese version in English: the Japanese
disc's cutscenes, translated from the Japanese.


ON MISTER
---------

Copy the contents of out/mister/ to the root of the MiSTer SD card:

    _Arcade/_Arcade Patches/_Enhanced Versions/         English restoration MRA
    _Arcade/_Arcade Patches/_Enhanced Versions/_USA/    USA MRA
    _Arcade/_Arcade Patches/_Enhanced Versions/_Japan/  Japan MRA
    _Arcade/cores/jtcps2-cpsplus_20261006.rbf           the CPS+ core
    games/hbmame/<set>.zip                              the sets
    games/CPS+/<pack>.zip                               the music packs

These sets need the CPS+ core included here, not the standard jtCPS2 core,
and 128 MB of SDRAM. MiSTer uses the newest dated CPS+ core it finds. If
the QSound firmware was absent from your input, keep qsound.zip or
qsound_hle.zip in games/mame.

The MiSTer builds play the Sega CD's arranged soundtrack and voice the
cutscenes: the USA build with the US disc's voices, the Japan build with the
Japanese disc's, and the English restoration with a new English voice track.
The music packs are built from your discs. MUSIC on the test menu's EX page
switches back to the arcade music.


ON HBMAME
---------

The sets need their definitions compiled into HBMAME. Add the ROM_START
blocks and GAME lines from hbmame/definitions/ to
src/hbmame/drivers/cps2mis.cpp, add the set names to src/hbmame/hbmame.lst,
and build HBMAME. Start the games by set name. HBMAME plays the arcade music,
and the cutscenes keep their captions.


ON DARKSOFT
-----------

Copy a game's folder from out/darksoft/games/ to the games folder of your
Darksoft CPS2 multi card. Its flash files, key and NAME stay together. The
game runs unencrypted, as Phoenix sets do, so its key file is blank. Darksoft
plays the arcade music. The receipts in out/darksoft/receipts/ stay off the
card.


IN THE GAME
-----------

The test menu's EX page turns the bonus stages on or off, switches CUT SCENES
between the Sega CD's and the arcade's own, and switches ARTWORK to Capcom
Arcade Stadium's revised soda can and Uptown sign. REGULATION chooses
Jessica's outfit on Haggar's TV in the opening.

Stage select: hold attack and jump and press Start on the title screen. Pick
any round, or the ending.


LEGAL
-----

Unofficial fan project. Not affiliated with or endorsed by Capcom or Sega.
All game titles, characters and artwork remain the property of their owners.
The cutscene art and audio derive from Capcom's Final Fight CD. You must own
the discs and romsets used as inputs. The kit carries only our own code, the
English voice track, three small Capcom Arcade Stadium tiles, and the CPS+
core with its source and licenses (core/). Data that depends on another disc
or on Capcom Arcade Stadium is stored so that it rebuilds only from your own
disc and romset. Do not sell this kit or distribute it applied to game
images.

ROM COLLECTIONS
---------------
Merged, split and complete ZIP/7z sets and extracted folders are accepted.
Use --rompath DIR (repeatable) or --romset FILE (repeatable). Names may
differ; required contents are verified by strong hashes. 7z needs installed
7-Zip. Use --check to preflight arcade inputs without supplying the discs.
--include-devices puts the QSound firmware into the sets. Source archives are
never modified.

MEDIA TOOLS

FFmpeg/ffprobe are fetched as pinned verified binaries. No separate FFmpeg
installation is needed. To work offline, add --tools-bundle
cpsplus-tools-v2-offline.zip --offline. Use --tools-cache to choose the cache.
NumPy and Pillow remain required.
