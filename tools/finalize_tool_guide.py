#!/usr/bin/env python3
"""Replace one prepublication status sentence in a bundled tool guide."""
import argparse
import hashlib
from pathlib import Path
import zipfile

OLD = b'Source and binary release assets are prepared for review and remain unpublished.'
NEW = (b'Source and binary release assets are available at:\n'
       b'https://github.com/strygo/arcade-patches/releases/tag/optional-helpers-v1')


def finalize(source, destination, member):
    if destination.exists():
        raise ValueError('Fresh finalized kit required')
    if member not in ('builder/TOOLS.md', 'cpsplus/TOOLS.md'):
        raise ValueError('Only the declared tool guide may change')
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        files = {i.filename: archive.read(i) for i in infos}
    if member not in files or files[member].count(OLD) != 1:
        raise ValueError('Expected preparation-status sentence is missing or ambiguous')
    before = dict(files)
    files[member] = files[member].replace(OLD, NEW)
    if [n for n in files if files[n] != before[n]] != [member]:
        raise ValueError('Unrelated kit change')
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for info in infos:
            archive.writestr(info, files[info.filename], compresslevel=9)
    with zipfile.ZipFile(destination) as archive:
        for name, data in before.items():
            if name != member and archive.read(name) != data:
                raise ValueError('Executable or game payload changed')
    print('PASS tool-guide-only finalization: ' + member)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--member', required=True)
    args = parser.parse_args()
    finalize(args.source, args.out, args.member)
