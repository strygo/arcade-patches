#!/usr/bin/env python3
"""Strict, original-key CPS-2 Darksoft export. No ROM data is bundled.

Mappings are explicit byte placements, never inferred from ROM filenames.
Callers must verify reconstructed ROM identities before calling export().
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
import zipfile

FLASH_SIZE = 0x400000
HERE = Path(__file__).resolve().parent


def require(ok, message):
    if not ok:
        raise ValueError('Darksoft: ' + message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def format_menu(title, region):
    """Pack convention: fourteen printable title characters, then =region."""
    require(isinstance(title, str) and re.fullmatch(r'[\x20-\x7e]{1,14}', title),
            'menu title must fit fourteen printable ASCII characters')
    require(region in ('U', 'J', 'E', 'A', 'W', 'B', 'C'), 'unsupported menu region')
    return title.ljust(14) + '=' + region


def native_menu(edition, region):
    """Approved native edition names; region describes the built presentation."""
    titles = {
        'sfa2-gold': {'us': 'SFAlpha2 Gold', 'eu': 'SFAlpha2 Dash',
                      'jp': 'SFZero 2 Dash', 'asia': 'SFZero 2 Dash'},
        'sfa2-ex': {'us': 'SFAlpha 2 EX', 'jp': 'SFZero 2 EX'},
        'usf2-backport': {'us': 'Ultra SFII', 'jp': 'Ultra SFII'},
    }
    require(edition in titles and region in titles[edition], 'unsupported native menu edition/region')
    return format_menu(titles[edition][region], {'us':'U','jp':'J','eu':'E','asia':'A'}[region])


def profile(setname):
    catalog = json.loads((HERE / 'darksoft_profiles.json').read_text())
    require(catalog['schema'] == 1, 'unsupported profile schema')
    require(setname in catalog['profiles'], 'unsupported CPS-2 layout: ' + setname)
    return catalog['profiles'][setname]


def validate(mapping):
    """Prove every member byte is mapped once and every flash span is disjoint."""
    members, files, key = mapping['members'], mapping['files'], mapping['key']
    require(key in members and members[key] == 20, 'expected one 20-byte key')
    require({'flash.01', 'flash.02', 'flash.03'} <= set(files), 'missing mandatory flash banks')
    source = {name: [] for name in members if name != key}
    for name, size in members.items():
        require(re.fullmatch(r'[A-Za-z0-9_.-]+', name) and type(size) is int and size > 0,
                'invalid member declaration')
    for flash, spans in files.items():
        require(re.fullmatch(r'flash\.(0[1-9]|1[0-2])', flash) and spans, 'invalid flash bank')
        occupied = []
        for name, start, target, size in spans:
            require(name in source and all(type(n) is int for n in (start, target, size)),
                    'invalid placement')
            require(start >= 0 and target >= 0 and size > 0 and
                    start + size <= members[name] and target + size <= FLASH_SIZE,
                    'placement exceeds member or flash capacity')
            source[name].append((start, start + size))
            occupied.append((target, target + size))
        occupied.sort()
        require(all(a[1] <= b[0] for a, b in zip(occupied, occupied[1:])), 'overlapping flash spans')
    for name, spans in source.items():
        spans.sort()
        require(spans and spans[0][0] == 0 and spans[-1][1] == members[name] and
                all(a[1] == b[0] for a, b in zip(spans, spans[1:])),
                'unmapped or multiply mapped member bytes: ' + name)


def unpack(files, mapping):
    """Independent read-back by source offsets, with strict FF-hole checks."""
    validate(mapping)
    require(set(files) == set(mapping['files']) | {'key'}, 'flash/key inventory differs')
    require(len(files['key']) == 20, 'key length differs')
    result = {n: bytearray(s) for n, s in mapping['members'].items()}
    result[mapping['key']][:] = files['key']
    for flash, spans in mapping['files'].items():
        data = files[flash]
        require(len(data) == FLASH_SIZE, 'flash size differs: ' + flash)
        end = 0
        for name, start, target, size in sorted(spans, key=lambda s: s[2]):
            require(data[end:target] == b'\xff' * (target - end), 'non-FF padding: ' + flash)
            result[name][start:start + size] = data[target:target + size]
            end = target + size
        require(data[end:] == b'\xff' * (FLASH_SIZE - end), 'non-FF padding: ' + flash)
    return {n: bytes(b) for n, b in result.items()}


def check_members(members, mapping):
    validate(mapping)
    extra = set(members) - set(mapping['members'])
    require(extra <= {'dl-1425.bin'}, 'unknown members (expanded hardware is unsupported): ' + ', '.join(sorted(extra)))
    require(set(mapping['members']) <= set(members), 'incomplete set; compact HBMAME overlays are unsupported')
    for name, size in mapping['members'].items():
        require(len(members[name]) == size, 'unsupported size/capacity: ' + name)
    if 'dl-1425.bin' in members:
        require(sha(members['dl-1425.bin']) == '524a25bcd153be24594d37c0dbd334fe4f384f77da73c2058dc60fc1f581cf18',
                'incorrect QSound device firmware')


def export(members, setname, *, menu):
    """Return flat flash/key/NAME files plus a separate hash-bound receipt.

    menu is an explicitly supplied ASCII menu label of at most 16 characters.
    No re-keying, decryption, byte swapping or game modification takes place.
    """
    mapping = profile(setname)
    check_members(members, mapping)
    require(isinstance(menu, str) and re.fullmatch(r'[\x20-\x7e]{1,16}', menu),
            'menu label must be 1..16 printable ASCII characters')
    require(members[mapping['key']] != b'\xff' * 20, 'Phoenix/dead-key sets are unsupported; the original key is required')
    files = {'key': members[mapping['key']]}
    for flash, spans in mapping['files'].items():
        data = bytearray(b'\xff' * FLASH_SIZE)
        for name, start, target, size in spans:
            data[target:target + size] = members[name][start:start + size]
        files[flash] = bytes(data)
    require(unpack(files, mapping) == {n: members[n] for n in mapping['members']}, 'read-back differs')
    files['NAME'] = menu.encode('ascii').ljust(16, b' ')
    receipt = {'schema': 1, 'status': 'verified_format_only', 'layout': setname,
               'profile_sha256': sha(json.dumps(mapping, sort_keys=True).encode()),
               'inputs': {n: sha(members[n]) for n in sorted(mapping['members'])},
               'files': {n: {'size': len(b), 'sha256': sha(b)} for n, b in sorted(files.items())},
               'hardware_review': False,
               'limitations': 'Original-key CPS-2 layout only. No expanded hardware or streamed audio.'}
    return files, receipt


def tree(members, setname, *, folder, menu):
    """Files ready for the existing transactional kit publisher."""
    require(re.fullmatch(r'[A-Za-z0-9_-]+', folder), 'unsafe game folder')
    files, receipt = export(members, setname, menu=menu)
    result = {f'darksoft/games/{folder}/{n}': b for n, b in files.items()}
    result[f'darksoft/receipts/{folder}.json'] = (json.dumps(receipt, indent=2, sort_keys=True) + '\n').encode()
    return result


def configure_ips(manifest, slug):
    """Bind only explicitly supported kit editions; unknown products fail closed."""
    catalog = json.loads((HERE / 'darksoft_kits.json').read_text())
    row = catalog['ips'].get(slug)
    if row is None:
        manifest['darksoft_unsupported'] = catalog['exceptions'].get(slug, 'No compatible CPS-2 layout has been declared.')
        return
    require(manifest['set'] == row['profile'] and manifest.get('mame_build', True),
            'kit no longer has its declared stock-layout output')
    mapping = profile(row['profile'])
    for variant in manifest.get('variants', [manifest]):
        sizes = {m['name']: m.get('output_size', m['size']) for m in variant['members']
                 if m['name'] != 'dl-1425.bin' and m.get('role') != 'device'}
        require(sizes == mapping['members'], 'kit member layout changed: ' + slug)
        key = variant.get('key', 'default')
        require(key in row['menus'], 'undeclared kit variant: ' + key)
        variant['darksoft'] = {'profile': row['profile'],
                              'folder': slug + ('_' + key if key != 'default' else ''),
                              'menu': row['menus'][key]}


def check_destination(root, outputs):
    """Never leave stale flash banks or overwrite a different game in place."""
    folders = {Path(n).parts[2] for n in outputs if n.startswith('darksoft/games/')}
    for folder in folders:
        prefix = 'darksoft/games/' + folder + '/'
        target = Path(root) / 'darksoft/games' / folder
        require(not target.is_symlink(), 'game destination is a symlink')
        if not target.exists():
            continue
        require(target.is_dir(), 'game destination is not a directory')
        expected = {n[len(prefix):]: b for n, b in outputs.items() if n.startswith(prefix)}
        actual = {p.name: p for p in target.iterdir()
                  if not (p.name.startswith('._') or p.name == '.DS_Store')}
        require(set(actual) == set(expected), 'existing game has stale/foreign files; choose a fresh output folder')
        require(all(p.is_file() and not p.is_symlink() and p.read_bytes() == expected[n]
                    for n, p in actual.items()), 'existing game differs; choose a fresh output folder')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--self-test', action='store_true')
    ap.add_argument('--set')
    ap.add_argument('--rom', type=Path, help='complete, identity-verified patched MAME ZIP')
    ap.add_argument('--out-dir', type=Path)
    ap.add_argument('--folder')
    ap.add_argument('--menu')
    args = ap.parse_args()
    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.discover(str(HERE), 'test_darksoft.py')
        require(suite.countTestCases() > 0, 'self-tests require the source checkout test_darksoft.py')
        require(unittest.TextTestRunner().run(suite).wasSuccessful(), 'self-test failed')
        return
    if not all((args.set, args.rom, args.out_dir, args.folder, args.menu)):
        ap.error('--set, --rom, --out-dir, --folder and --menu are required')
    require(not args.out_dir.exists(), 'choose a fresh --out-dir')
    with zipfile.ZipFile(args.rom) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), 'duplicate ZIP members')
        members = {n: archive.read(n) for n in names}
    from rom_sources import publish_tree
    publish_tree(args.out_dir, tree(members, args.set, folder=args.folder, menu=args.menu))


if __name__ == '__main__':
    main()
