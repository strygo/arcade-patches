"""Shared native collection interface for PS2 Arrange reconstruction kits.

The standalone entry point owns reconstruction. This module owns exact binding,
complete runtime layouts and descriptions generated from native packaging output.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import zipfile

from kit_collection import require, safe_name, sha, contracts, load_bindings, read_binding, verify_outputs
from rom_sources import QSOUND, publish_tree


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 ** 2), b''):value.update(block)
    return value.hexdigest()


def encoded(value):return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def tree_outputs(root):
    files = {}
    for path in sorted(Path(root).rglob('*')):
        if not path.is_file():continue
        name = path.relative_to(root).as_posix()
        if path.suffix == '.zip':
            with zipfile.ZipFile(path) as archive:
                require(len(archive.namelist()) == len(set(archive.namelist())), 'Duplicate native ZIP member')
                files[name] = {n: archive.read(n) for n in archive.namelist()}
        else:files[name] = path.read_bytes()
    return files


def regions(root):
    root = Path(root)
    filename = 'reconstruction.json' if (root / 'reconstruction.json').exists() else 'kit.json'
    return json.loads((root / filename).read_text())['regions']


def raw_region(native, root, region):
    meta = regions(root)[region];setname = meta['set']
    return {name: spec for name, spec in native.items() if
        name in (f'hbmame/{setname}.zip', f'mister/games/hbmame/{setname}.zip') or
        name.startswith(('mister/_Arcade/cores/', 'mister/games/CPS+/')) or
        name.endswith('.mra') and (('/_Japan/' in name) == (region == 'jp'))}


def project(root, project_id, region, platform, native):
    """One complete output projection, shared by description and runtime export."""
    require(platform in ('hbmame', 'mister'), 'Unknown Arrange platform')
    mapping = {name: 'roms/' + Path(name).name if platform == 'hbmame' else name[len('mister/'):]
               for name in raw_region(native, root, region) if name.startswith(platform + '/')}
    support = {'README.txt': f'support/{project_id}/README.txt'}
    if platform == 'hbmame':support['hbmame-sets.cpp'] = f'support/{project_id}/hbmame-sets.cpp'
    else:
        core = json.loads((Path(root) / 'kit.json').read_text())['core']
        for path in sorted((Path(root) / 'core').rglob('*')):
            if path.is_file() and path.suffix != '.rbf':
                name = path.relative_to(Path(root) / 'core').as_posix()
                support['core/' + name] = '_Arcade/cores/licenses/' + core + '/' + name
    return mapping, support


def define(root, project_id, version, revision, native, disc):
    root = Path(root);prerequisites = {};inputs = []
    def add(title, kind, pin):
        key = kind + '-' + pin['sha256'][:20]
        prerequisites[key] = {'title': title, 'kind': kind, 'formats': ['loose file', 'ZIP member', '7z member'],
            'alternatives': [pin], 'action': 'Add your canonical arcade ROMs, QSound firmware and owned Vampire: Darkstalkers Collection disc to scanned folders.'}
        inputs.append(key);return key
    sets = json.loads((root / 'rom_inputs.json').read_text())['sets']
    config = json.loads((root / 'kit.json').read_text())
    wanted = config.get('romsets', {name: list(rows) for name, rows in sets.items()})
    for name, members in wanted.items():
        for member in members:
            row = sets[name][member]
            add(name + ': ' + member, 'rom', {k: row[k] for k in ('size', 'sha256', 'crc32') if k in row})
    firmware = {'size': QSOUND['size'], 'sha256': QSOUND['sha256'], 'crc32': QSOUND['crc32']}
    add('QSound firmware (dl-1425.bin)', 'rom', firmware)
    disc_id = add('Vampire: Darkstalkers Collection (Japan), PlayStation 2 disc', 'disc', disc)
    prerequisites[disc_id]['formats'] = ['loose DVD CHD or ISO']
    targets = {}
    for region in regions(root):
        for platform in ('hbmame', 'mister'):
            mapping, support = project(root, project_id, region, platform, native)
            outputs = {new: native[old] for old, new in mapping.items()}
            outputs.update({new: {'sha256': digest(root / old)} for old, new in support.items()})
            if platform == 'hbmame':outputs['roms/qsound.zip'] = {'members': {'dl-1425.bin': firmware['sha256']}}
            targets[region + '-' + platform] = {'platform': platform, 'variant': region, 'region': region,
                'inputs': sorted(set(inputs)), 'outputs': outputs, 'minimum_free_bytes': 12 * 1024 ** 3,
                'destination_requirements': 'HBMAME with the included set definitions.' if platform == 'hbmame'
                    else 'Includes the unchanged CPS+ core and shared Collection music pack.'}
    return {'api': 1, 'project': project_id, 'game_version': version, 'kit_revision': revision,
            'requires': ['native-context-v1', 'native-bindings-v1', 'disc-inputs-v1', 'run-cache-v1'],
            'prerequisites': prerequisites, 'targets': targets, 'native_outputs': native}


def seal(root, project_id, version, revision, native, disc):
    root = Path(root);definition = define(root, project_id, version, revision, native, disc)
    manifest = {'api': 1, 'project': project_id, 'version': version, 'kit_revision': revision,
        'files': {p.relative_to(root).as_posix(): digest(p) for p in sorted(root.rglob('*')) if p.is_file()
                  and p.relative_to(root).as_posix() not in ('manifest.json', 'collection.json') and '__pycache__' not in p.parts}}
    (root / 'manifest.json').write_bytes(encoded(manifest));definition['manifest_sha256'] = digest(root / 'manifest.json')
    (root / 'collection.json').write_bytes(encoded(definition));return definition


def describe(root):
    root = Path(root);value = json.loads((root / 'collection.json').read_text())
    require(value['api'] == 1 and value['manifest_sha256'] == digest(root / 'manifest.json'), 'Arrange definition manifest differs')
    manifest = json.loads((root / 'manifest.json').read_text())
    require(value['project'] == manifest['project'] and value['game_version'] == manifest['version']
            and value['kit_revision'] == manifest['kit_revision'], 'Arrange definition identity differs')
    for name, expected in manifest['files'].items():safe_name(name);require(digest(root / name) == expected, 'Arrange producer changed: ' + name)
    return value


def decode(binding, cache):
    require('member' not in binding, 'Supply a loose DVD CHD or ISO')
    source = Path(binding['path'])
    require(source.is_absolute() and source.stat().st_size == binding['size'] and digest(source) == binding['sha256'], 'Disc changed since discovery')
    if binding.get('format') == 'iso':return source
    with source.open('rb') as stream:header = stream.read(124)
    require(len(header) == 124 and header[:8] == b'MComprHD' and int.from_bytes(header[12:16], 'big') == 5, 'Expected CHD v5 DVD')
    size = int.from_bytes(header[32:40], 'big');require(0 < size <= 9 * 1024 ** 3, 'Invalid DVD size')
    cache = Path(cache) / 'discs';cache.mkdir(parents=True, exist_ok=True)
    output = cache / (binding['sha256'] + '.iso');marker = output.with_suffix('.json')
    if output.is_file() and marker.is_file():
        metadata = json.loads(marker.read_text())
        require(metadata['source_sha256'] == binding['sha256'] and output.stat().st_size == size and digest(output) == metadata['iso_sha256'], 'Decoded disc cache changed')
        return output
    from fetch_optional_tools import disc_tool
    import subprocess
    tool = Path(disc_tool('chdman'))
    require(shutil.disk_usage(cache).free > size + 1024 ** 3, 'Insufficient DVD decode space')
    with tempfile.TemporaryDirectory(prefix='decode-', dir=cache) as temporary:
        decoded = Path(temporary) / 'disc.iso'
        subprocess.run([str(tool), 'extractdvd', '-i', str(source), '-o', str(decoded)], check=True, timeout=900)
        require(decoded.stat().st_size == size, 'Decoded DVD size differs')
        metadata = {'source_sha256': binding['sha256'], 'iso_sha256': digest(decoded), 'chdman_sha256': digest(tool)}
        decoded.replace(output);marker.write_bytes(encoded(metadata))
    return output


def run(root, args):
    root = Path(root);definition = describe(root);target_id = args.variant + '-' + args.platform
    require(target_id in definition['targets'], 'Unknown Arrange collection target')
    value = json.loads(args.inputs_json.read_text());load_bindings(args.inputs_json)
    target = definition['targets'][target_id];bindings = value['bindings'];context = value['context']
    require(set(bindings) == set(target['inputs']), 'Incomplete or unexpected bound Arrange inputs')
    os.environ.update(CPSPLUS_SWITCH_TOOLS_CACHE=context['tools_cache'], ARCADE_PYTHON_CACHE=str(Path(context['tools_cache']) / 'python'))
    if context['offline']:os.environ['CPSPLUS_SWITCH_TOOLS_OFFLINE'] = '1'
    else:os.environ.pop('CPSPLUS_SWITCH_TOOLS_OFFLINE', None)
    os.environ.pop('CAPCOM_ARCADE_ROM_PATH', None);os.environ.pop('CPSPLUS_PYTHON', None)
    data = {};disc = None
    for key, binding in bindings.items():
        requirement = definition['prerequisites'][key]
        require(any(binding['size'] == p['size'] and binding['sha256'] == p['sha256'] for p in requirement['alternatives']), 'Undeclared Arrange source')
        if requirement['kind'] == 'disc':disc = binding
        else:data[binding['sha256']] = read_binding(binding)
    require(disc is not None, 'Missing owned disc')
    iso = decode(disc, context['run_cache'])
    previous = sys.argv
    try:
        sys.argv = [str(root / 'apply.py')]
        producer = runpy.run_path(str(root / 'apply.py'))
    finally:sys.argv = previous
    producer['check_native_disc'](iso)
    roms = {}
    catalog = json.loads((root / 'rom_inputs.json').read_text())['sets']
    wanted = json.loads((root / 'kit.json').read_text()).get('romsets', {name: list(rows) for name, rows in catalog.items()})
    for name, members in wanted.items():roms[name] = {member: data[catalog[name][member]['sha256']] for member in members}
    firmware = data[QSOUND['sha256']]
    operation = 'check' if args.check else 'build'
    if not args.check:
        identity = sha(encoded({'manifest': definition['manifest_sha256'], 'region': args.variant,
                               'inputs': {k: b['sha256'] for k, b in bindings.items()}}))
        cache = Path(context['run_cache']) / ('arrange-' + identity);cache.mkdir(parents=True, exist_ok=True)
        marker = cache / 'complete.json';raw = cache / 'raw'
        expected = raw_region(definition['native_outputs'], root, args.variant)
        if marker.exists():
            require(json.loads(marker.read_text()) == expected, 'Arrange same-run cache identity differs')
            files = {}
            require({p.relative_to(raw).as_posix() for p in raw.rglob('*') if p.is_file()} == set(expected), 'Arrange cache extent differs')
            for path in sorted(raw.rglob('*')):
                if not path.is_file():continue
                name = path.relative_to(raw).as_posix()
                if path.suffix == '.zip':
                    with zipfile.ZipFile(path) as archive:
                        require(len(archive.namelist()) == len(set(archive.namelist())), 'Duplicate cached member')
                        files[name] = {n: archive.read(n) for n in archive.namelist()}
                else:files[name] = path.read_bytes()
        else:
            files = producer['build_native_outputs'](iso, roms, firmware, args.variant)
            verify_outputs(files, expected);publish_tree(raw, files);marker.write_bytes(encoded(expected))
        verify_outputs(files, expected)
        mapping, support = project(root, definition['project'], args.variant, args.platform, definition['native_outputs'])
        outputs = {new: files[old] for old, new in mapping.items()}
        outputs.update({new: (root / old).read_bytes() for old, new in support.items()})
        if args.platform == 'hbmame':outputs['roms/qsound.zip'] = {'dl-1425.bin': firmware}
        verify_outputs(outputs, target['outputs']);publish_tree(args.out_dir, outputs)
    return {'api': 1, 'operation': operation, 'target': target_id, 'status': 'ok', **({'outputs': target['outputs']} if not args.check else {})}


def main(root):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--describe-json', action='store_true');parser.add_argument('--collection-layout', action='store_true')
    parser.add_argument('--inputs-json', type=Path);parser.add_argument('--report-json', type=Path)
    parser.add_argument('--platform');parser.add_argument('--variant');parser.add_argument('--out-dir', type=Path);parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.describe_json:print(json.dumps(describe(root), sort_keys=True));return 0
    require(args.collection_layout and args.inputs_json and args.report_json and args.out_dir, 'Use exact bound collection inputs')
    try:result = run(root, args)
    except (ValueError, OSError, KeyError) as exc:
        args.report_json.write_bytes(encoded({'api': 1, 'operation': 'check' if args.check else 'build',
            'target': str(args.variant) + '-' + str(args.platform), 'status': 'error', 'message': str(exc)}));return 2
    args.report_json.write_bytes(encoded(result));return 0
