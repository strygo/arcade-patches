STRIDER EX — RECONSTRUCTION KIT
===============================

Build Strider EX from your own arcade ROMs and Strider Hiryuu PC Engine CD.
The kit includes the Strider EX CPS+ core and reconstructs the arranged
soundtrack and English/Japanese voices. It contains no original game ROMs,
extracted scenes, original disc tracks or built audio packs.

REQUIREMENTS
------------
Python 3.11 or later, NumPy, Pillow and SciPy:

    python3 -m pip install numpy Pillow scipy

MAME 0.288 with Lua support is required to replay the source scenes and
measure the original video state. Runs are automatic and use no window.
The first reconstruction takes longer than FFEX: it recreates the source
measurements before converting them to CPS-1 tiles. Allow 20 GiB of scratch.

The native player code is included as verified assembly payloads. The kit
reconstructs their scene data from your game inputs using Python. No assembler,
Git, make or C compiler is needed. See TOOLS.md for the source pins.

Your game inputs:

- Canonical MAME Strider sets: strider, striderua and striderjr. ZIP, 7z or
  loose files in one ROM directory; merged archives are also accepted.
  Both regional inputs are needed for the shared graphics conversion even
  when selecting one output region.
- Strider Hiryuu (Japan), PC Engine CD, as a complete Redump split-bin CUE
  rip or a ZIP/7z containing the CUE and every track. Preserve track names.
- The unheadered Japanese Super CD-ROM System v3.0 BIOS (262144 bytes,
  SHA-1 79f5ff55dd10187c7fd7b8daab0b3ffbd1f56a2c).
- Strider Hiryuu (1992)(Capcom).zip, the X68000 disk archive used to restore
  the unused arcade music. This is required for both audio options.

7z archives require 7-Zip (7zz or 7z). CPS+ encoding obtains content-pinned
FFmpeg helpers automatically. --tools-cache selects their shared cache;
--tools-bundle and --offline support the corresponding offline helper bundle.

BUILD
-----
    python3 apply.py --disc "Strider Hiryuu (Japan).7z" \
      --romset /path/to/roms --bios /path/to/system-card.pce \
      --x68k "Strider Hiryuu (1992)(Capcom).zip" --out-dir out

Use --mame /path/to/mame when MAME is not on PATH. --check verifies the kit
and owned inputs without starting the reconstruction. --work selects a
scratch parent; the build removes its own temporary directory on exit.
--jobs limits simultaneous captures (default 3).

--region both (default), us or jp selects outputs.
--platform both (default), mister or hbmame selects targets.
--audio both (default), arcade or cpsplus selects MiSTer launchers.

All reconstructed files are verified before installation. A conflicting
existing file is preserved and reported; an identical file can be reused.

OUTPUTS
-------
Copy the contents of out/mister to the MiSTer card root:

    _Arcade/_Arcade Patches/_Enhanced Versions/Strider EX (USA).mra
    _Arcade/_Arcade Patches/_Enhanced Versions/_Japan/Strider Hiryu EX (Japan).mra
    _Arcade/_Arcade Patches/_Arrangements/_CPS+/Strider EX (USA) [Arrange].mra
    _Arcade/_Arcade Patches/_Arrangements/_CPS+/Strider EX (USA) [Arrange, Japanese voices].mra
    _Arcade/_Arcade Patches/_Arrangements/_CPS+/_Japan/Strider Hiryu EX (Japan) [Arrange].mra
    _Arcade/_Arcade Patches/_Arrangements/_CPS+/_Japan/Strider Hiryu EX (Japan) [Arrange, English voices].mra
    _Arcade/cores/jtcps1-striderex-cpsplus.rbf
    games/hbmame/striderus01.zip
    games/hbmame/striderjs01.zip
    games/CPS+/strider_ex.zip

Only selected outputs are written. The launchers share the regional ROM
ZIPs. The USA defaults to English voices; Japan defaults to Japanese.
Arcade audio uses the original FM/OKI hardware, including the new native
cinematic score and embedded English boss dialogue in the USA build.
CPS+ adds the PC Engine arranged soundtrack and full-quality English boss
recordings, plus voiced cinematics in either language.

Every MiSTer launcher needs the included core: it supplies the Strider EX
graphics mapper as well as CPS+ audio. Its distinct filename preserves an
installed generic CPS+ core. Do not rename it or bind these MRAs to jtcps1.

out/hbmame contains standalone striderus01 / striderjs01 ZIPs and matching
ROM_START/GAME registration fragments for HBMAME. Stock MAME does not run
these enhanced arcade sets. HBMAME has no CPS+ player and uses native audio.

STAGE SELECT
------------
Insert a coin. Hold attack and jump while pressing Start on the title
screen. Hiryu's claw sound confirms the code. Release the buttons, choose
with the joystick, then press attack, jump or Start. Stage-select runs do
not enter the high-score table. The six entries follow the normal stage
order, including Kyzylkum Desert.

Run python3 verify.py --out-dir out to independently check the reconstructed
files, MRA assembly, audio options, soundtrack CRCs and core binding.
For a partial build, pass the same --region, --audio and --platform selections
to verify.py.

LICENSES
--------
See LICENSES.txt, core/SOURCE_AND_LICENSE.txt, core/LICENSE and TOOLS.md. The kit carries
reconstruction source, measurement recipes, original English voice material,
a disc-dependent English dub delta, and the open-source FPGA core. Supply
your own game media. Do not redistribute the reconstructed ROMs or packs.
