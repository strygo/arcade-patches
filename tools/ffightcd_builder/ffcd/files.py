"""File helpers that behave the same on every filesystem the kit runs on."""
import os
import shutil


def link_or_copy(src, dst) -> None:
    """Hardlink a repeated frame; copy it where links aren't possible.

    FAT32/exFAT drives, cross-drive paths and NTFS's 1023-links-per-file cap
    all refuse os.link, so a copy (same bytes, more space) is the fallback."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)
