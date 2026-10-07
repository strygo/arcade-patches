{TITLE} — reconstruction kit
{RULE}

Version:      (stamped at release)
Hardware:     Capcom CPS-2 with the CPS+ extensions
Target:       MiSTer (CPS+ core, included) and HBMAME — USA and Japan
Backport by:  Steve Gordon (https://x.com/strygo)
Website:      https://arcadepatches.com/{SLUG}/

This kit builds {TITLE}, a CPS-2 backport of the Arrange edition from
Capcom's PlayStation 2 Vampire: Darkstalkers Collection, from your own disc
image and arcade romsets. Original game ROMs are not included.

WHAT YOU NEED
-------------

  1. Your PlayStation 2 disc image:
     "Vampire: Darkstalkers Collection" (Japan).
  2. Your arcade romsets, all Japanese:
       - vhunt2.zip  Vampire Hunter 2
       - vsav2.zip   Vampire Savior 2
       - vsavj.zip   Vampire Savior (its sound ROMs; a merged or split set
                     with vsav.zip works too)
  3. Python 3.10 or newer. Nothing else — no emulator, no assembler.

USAGE
-----

    python3 apply.py --iso <collection.iso> --rompath <romset folder> \
                     --out-dir out

This builds both regions (add --region us or --region jp for one) and creates:

    out/hbmame/<set>.zip    HBMAME
    out/mister/             ready to copy to a MiSTer SD card

The kit verifies every finished file automatically. It stops with an error if
the disc or a romset does not match.

SETS
----

{SETS}

ON MISTER
---------

Copy the contents of out/mister/ to the root of the MiSTer SD card:

    _Arcade/_Arcade Patches/_Restorations/<name>.mra          USA
    _Arcade/_Arcade Patches/_Restorations/_Japan/<name>.mra   Japan
    _Arcade/cores/jtcps2-cpsplus_20261006.rbf                  the CPS+ core
    games/hbmame/<set>.zip                                     the sets

These sets need the CPS+ core included here, not the standard jtCPS2 core,
and 128 MB of SDRAM. MiSTer uses the newest dated CPS+ core it finds.
If QSound was absent from your input, keep qsound.zip or qsound_hle.zip in
games/mame.

ON HBMAME
---------

The sets need HBMAME's CPS+ machine driver. Start them by set name.

LEGAL
-----

Unofficial fan backport. Not affiliated with or endorsed by Capcom. All game
titles, characters, and artwork remain the property of their owners. You must
own the disc and romsets used as inputs. The recipes carry only our own work:
code, translated text and layout. Do not sell this kit or distribute it
applied to game images. The CPS+ core's source and licenses are in core/.

ROM COLLECTIONS
---------------
Merged, split and complete ZIP/7z sets and extracted folders are accepted.
Use --rompath DIR (repeatable) or --romset FILE (repeatable). Names may
differ; required contents are verified by strong hashes. 7z needs installed
7-Zip. Use --check to preflight arcade inputs without supplying the disc.
Source archives are never modified.
