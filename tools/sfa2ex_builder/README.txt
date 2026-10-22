Street Fighter Alpha 2 EX / Zero 2 EX — reconstruction kit
==========================================================

Version:      (stamped at release)
Hardware:     Capcom CPS-2
Target:       MAME and HBMAME sets, and MiSTer (jtcps2) — USA and Japan
Project by:   Steve Gordon (https://x.com/strygo)
Website:      https://arcadepatches.com/sfa2-ex/

This kit builds Street Fighter Alpha 2 EX, an enhanced CPS-2 edition of
Street Fighter Alpha 2 Gold / Zero 2 Dash, from your own PlayStation 2 disc
image and arcade romset. Original game ROMs are not included.

WHAT YOU NEED
-------------

  1. Your PlayStation 2 anthology disc image:
       - USA   : "Street Fighter Alpha Anthology" (USA)
       - Japan : "Street Fighter Zero - Fighter's Generation" (Japan)
  2. Your arcade Street Fighter Zero 2 Alpha romset:
       - sfz2al.zip  for the USA set
       - sfz2alj.zip for the Japan set
  3. Python 3.10 or newer. Nothing else — no emulator, no assembler.

USAGE (recommended — build every platform for one region)
----------------------------------------------------------

    python3 apply.py --iso <anthology.iso> --romset <sfz2al.zip> \
                     --region <us|jp> --out-dir out

This creates:

    out/mame/<region>/<set>.zip  4MB — stock MAME
    out/darksoft/games/<game>/   8MB — best native samples, Darksoft flash format

    Use --platform darksoft --out-dir out for Darksoft only. Copy the game
    folder to the card games folder. This matches HBMAME's best native
    sample version. No duplicate 4 MiB alternative or streamed audio is exported.
    out/hbmame/<set>.zip         8MB — HBMAME, with every sound uncompressed
    out/mister/                  8MB — ready to copy to a MiSTer SD card

Use the same output folder when building both regions. Each region needs the
matching disc listed above.

REGIONS
-------

    us    Street Fighter Alpha 2 EX
    jp    Street Fighter Zero 2 EX (Japan)

USAGE (single file)
-------------------

    python3 apply.py --iso ... --romset ... --region us --size 8mb --out sfa2ex.zip

The kit verifies the finished files automatically. It stops with an error if
the supplied disc or romset does not match the selected region.

The 4MB sets keep the arcade set names (sfz2al, sfz2alj), the same names the
Street Fighter Alpha 2 Gold kit uses, so keep one of them at a time in your
MAME ROM path. MAME's game menu won't start a set with patched ROMs, so start
it from the command line: mame sfz2al (or mame sfz2alj). MAME shows checksum
warnings for the patched ROMs; that's expected and the game runs normally.
To start it from a menu, use the HBMAME build (sfa2ex, sfz2ex).

ON MISTER
---------

Copy the contents of out/mister/ to the root of the MiSTer SD card:

    _Arcade/_Arcade Patches/_Enhanced Versions/<name>.mra         the USA MRA
    _Arcade/_Arcade Patches/_Enhanced Versions/_Japan/<name>.mra  Japan
    games/hbmame/<set>.zip                                        the 8MB sets

The standard Jotego jtCPS2 core is required. If QSound was absent from your
input, keep qsound.zip or qsound_hle.zip in games/mame on MiSTer. MAME/HBMAME
can find it in their normal ROM paths.

GAME MODE
---------

The title-screen mode menu, EX Versus and OPTIONS appear in CONSOLE game mode.
The MiSTer MRAs start in CONSOLE. MAME and HBMAME start in ARCADE, which keeps
Gold's arcade title; set GAME MODE to CONSOLE on the SYSTEM page of the
operator menu.

CONSOLE FIGHTS
--------------

Press Coin to open Pause / Back during fights or character selection.
UP and DOWN select a row, LP confirms, and Coin or START resumes. Quit asks
for confirmation before returning to the title menu. Either joined player
can pause; the player who opened the menu controls it. START keeps its native
taunt and stage-selection behavior. Holding START during a fight also opens
pause. Music and sound resume with the fight.
Arcade mode keeps its normal coin behavior.

On Gen's character-select cell, START alternates his two starting stances.

TRAINING
--------

Choose TRAINING after VERSUS on the title menu. Select your fighter, then
the computer. NORMAL MODE is endless practice; RECORDING MODE records one
round, and REPLAY plays it from the saved setup and settings.

Press Coin to open Pause, then choose TRAINING MENU. Training Settings
controls the gauge,
stun, attack data and input history. Computer Settings controls movement,
guard, roll and throw escape. LP confirms menu rows; LEFT and RIGHT change
settings. RESTART keeps your chosen stage. Character Change returns to
selection; Quit returns to the title menu.

For a different stage, highlight its fighter during COMPUTER SELECT, hold
START for one second, move to the desired computer and confirm while still
holding START. Otherwise the computer's usual stage is used.

EX CONFIGURATION
----------------

The service menu's EX CONFIGURATION page has two independent choices:

  BACKGROUNDS  JAPAN / INTERNATIONAL (default JAPAN)
    JAPAN uses Sodom's Japanese truck lettering, Sakura's calligraphy and
    Dan's red-lettered upper sign. INTERNATIONAL uses English truck
    lettering, Sakura's Mt. Fuji plaque and Dan's plain sign.

  ARTWORK      ORIGINAL / 30TH ANNIVERSARY (default ORIGINAL)
    Selects the original or collection billboard artwork on Nash's stage.

LEFT and RIGHT change a setting. Return to the service root and use
SAVE & EXIT to keep it. DEFAULT restores JAPAN / ORIGINAL.

REGULATION
----------

The SYSTEM page's REGULATION setting starts OFF, keeping red blood.
ON switches to white. SAVE & EXIT preserves it independently of GAME MODE;
DEFAULT clears both settings.

LEGAL
-----

Unofficial fan edition. Not affiliated with or endorsed by Capcom. All game
titles, characters, and artwork remain the property of their owners. You must
own the disc and romset used as inputs. The recipes carry a few pieces of
Capcom data: about 4 KB of Cammy's theme score from X-Men vs. Street Fighter
(Capcom, 1996), which her stage music is built from; the infinity and star
symbols from Street Fighter Zero 2 Dash for the Sega Saturn; a few bytes of
projectile data from Super Street Fighter II Turbo; the altered billboard
artwork from Street Fighter 30th Anniversary Collection; and EX's mirrored and
recolored versions of the game's own art. Do not sell this kit or distribute
it applied to game images.

ROM COLLECTIONS
---------------
Merged, split and complete ZIP/7z sets and extracted folders are accepted.
Use --rompath DIR (repeatable) to search a collection. Names may differ;
required contents are verified by strong hashes. 7z needs installed 7-Zip.
Use --check to preflight arcade inputs without supplying the disc.
--out-dir builds all supported platforms; --platform selects just one.
Source archives are never modified.
QSound is optional when building. Valid embedded firmware is preserved.
--include-devices requires and includes firmware from any supplied archive.
