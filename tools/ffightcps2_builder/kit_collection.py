"""Kit-owned collection interface for the shared IPS builder (API 1).

Definitions are generated while packaging from accepted native outputs. They
are descriptions, not executable recipes. Release QA remains the independent
acceptance authority. This module uses only bundled code and the standard library.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import xml.etree.ElementTree as ET
import zipfile

API = 1
PLATFORMS = ('mame', 'hbmame', 'fbneo', 'mister', 'darksoft')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(value):
    require(isinstance(value, str) and value and '\\' not in value, 'Invalid relative output name')
    path = PurePosixPath(value)
    require(not path.is_absolute() and path.as_posix() == value and all(p not in ('.', '..') for p in path.parts),
            'Output path escapes export')
    return value


def load_bindings(path):
    value = json.loads(Path(path).read_text())
    require(value.get('schema') == 1 and isinstance(value.get('files'), list), 'Unknown bound-input schema')
    for row in value['files']:
        require(type(row.get('size')) is int and row['size'] >= 0 and
                re.fullmatch('[0-9a-f]{64}', row.get('sha256', '')) and Path(row['path']).is_absolute(),
                'Bound input needs an absolute path, size and SHA-256')
    return value['files']


def read_binding(row):
    path = Path(row['path'])
    if 'member' in row:
        with zipfile.ZipFile(path) as archive:
            matches = [i for i in archive.infolist() if i.filename == row['member']]
            require(len(matches) == 1 and not matches[0].is_dir() and matches[0].file_size == row['size'],
                    'Bound ZIP member missing, ambiguous or changed')
            data = archive.read(matches[0])
    else:
        require(path.stat().st_size == row['size'], 'Bound source size changed')
        data = path.read_bytes()
    require(len(data) == row['size'] and sha(data) == row['sha256'], 'Bound source content changed')
    return data


def resolve_bound(requirements, bindings, verify, optional=False):
    found = {}
    for spec in requirements:
        for row in bindings:
            if row['size'] != spec['size'] or (spec.get('sha256') and row['sha256'] != spec['sha256']):
                continue
            data = read_binding(row)
            if verify(data, spec):
                found[spec['name']] = data
                break
        require(optional or spec['name'] in found, 'Missing declared input: ' + spec['name'])
    return found


def contracts(outputs):
    return {name: {'members': {n: sha(data) for n, data in value.items()}} if isinstance(value, dict)
            else {'sha256': sha(value)} for name, value in outputs.items()}


def complete_outputs(base, variant, platform, outputs, stock):
    """One layout/ownership rule used by packaging and native execution."""
    require(platform in PLATFORMS, 'Select exactly one collection platform')
    result = {}
    for name, value in outputs.items():
        safe_name(name)
        require(name.startswith(platform + '/'), 'Unexpected native platform output')
        destination = ('roms/' + PurePosixPath(name).name if platform in ('mame', 'hbmame', 'fbneo')
                       else name[len(platform) + 1:])
        require(destination.casefold() not in {n.casefold() for n in result}, 'Collection output collision')
        result[destination] = value
    if platform in ('mame', 'hbmame', 'mister'):
        require('dl-1425.bin' in stock, 'Complete runtime export requires QSound firmware')
        if platform == 'mister' or (platform == 'hbmame' and not variant['hbmame'].get('complete')):
            name = ('games/mame/' if platform == 'mister' else 'roms/') + base['set'] + '.zip'
            require(name not in result, 'Stock parent conflicts with patched output')
            result[name] = dict(stock)
        if platform != 'mister':
            require('roms/qsound.zip' not in result, 'Device output collision')
            result['roms/qsound.zip'] = {'dl-1425.bin': stock['dl-1425.bin']}
    return result


def define(root, project, base, bundles, stock, build_outputs):
    """Produce kit metadata using the native builder, never an external adapter."""
    from rom_sources import QSOUND
    require(re.fullmatch('[a-z0-9][a-z0-9-]*', project), 'Invalid collection project ID')
    targets, prerequisites, native = {}, {}, {}
    for manifest, patches in bundles:
        variant = manifest.get('variant', {}).get('key') or 'default'
        specs = [m for m in manifest['members'] if m['action'] != 'add' and m.get('role') != 'device'
                 and m['name'] != 'dl-1425.bin']
        ids = []
        for spec in [*specs, QSOUND]:
            identity = spec.get('stock_sha256', spec.get('sha256'))
            require(identity and len(identity) == 64, 'Collection input requires SHA-256')
            key = 'source-' + identity[:20]
            prerequisites[key] = {'title': spec['name'], 'kind': 'device' if spec['name'] == 'dl-1425.bin' else 'rom',
                'formats': ['loose file', 'ZIP member', '7z member'],
                'action': 'Add the canonical ' + base['set'] + ' arcade set and QSound firmware to a media folder.',
                'alternatives': [{'size': spec['size'], 'sha256': identity}]}
            ids.append(key)
        source = {m['name']: stock[m['name']] for m in specs}
        source['dl-1425.bin'] = stock['dl-1425.bin']
        devices = {'dl-1425.bin': stock['dl-1425.bin']} if base.get('mame_include_devices', True) else {}
        raw = build_outputs(base, [(manifest, patches)], source, devices, 'all')
        native.update(contracts(raw))
        region = None
        if manifest.get('mra'):
            xml = ET.fromstring((Path(root) / manifest['mra']).read_bytes())
            text = (xml.findtext('region') or '').strip().lower()
            region = {'usa': 'us', 'japan': 'jp', 'europe': 'eu'}.get(text, text)
            require(re.fullmatch('[a-z0-9-]+', region or ''), 'MRA requires a declared output region')
        for platform in PLATFORMS:
            selected = {p: value for p, value in raw.items() if p.startswith(platform + '/')}
            if not selected:
                continue
            target = {'platform': platform, 'variant': variant,
                'native_variant': manifest.get('variant', {}).get('key'),
                'profile': project + '-' + variant if platform == 'mame' else 'default',
                'inputs': sorted(ids if platform in ('mame', 'hbmame', 'mister') else ids[:-1]),
                'outputs': contracts(complete_outputs(base, manifest, platform, selected, source))}
            target['destination_requirements'] = (
                'Use emulator definitions matching this release.' if platform in ('mame', 'hbmame', 'fbneo') else
                'Use the MiSTer core named by the included MRA.' if platform == 'mister' else
                "Use the Darksoft hardware and layout described in this kit's readme.")
            if region:
                target['region'] = region
            targets[variant + '-' + platform] = target
    return {'api': API, 'project': project, 'game_version': base['version'], 'kit_revision': base.get('kit_revision', 1),
        'manifest_sha256': sha((Path(root) / 'manifest.json').read_bytes()),
        'prerequisites': prerequisites, 'targets': targets, 'native_outputs': native}


def describe(root):
    root = Path(root)
    value = json.loads((root / 'collection.json').read_text())
    require(value.get('api') == API and value['manifest_sha256'] == sha((root / 'manifest.json').read_bytes()),
            'Kit definition differs from its manifest; regenerate it while packaging')
    return value


def verify_outputs(outputs, expected):
    require(contracts(outputs) == expected, 'Collection output differs from packaged contract')


def report(path, operation, target, status='ok', outputs=None, message=None):
    if path:
        result = {'api': API, 'operation': operation, 'target': target, 'status': status}
        if outputs is not None:
            result['outputs'] = outputs
        if message:
            result['message'] = message
        path = Path(path)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps(result, sort_keys=True) + '\n')
        temporary.replace(path)


def package_archive(path, project, sources):
    """Add a freshly generated definition as part of normal kit packaging."""
    import subprocess
    import sys
    import tempfile
    path = Path(path)
    with tempfile.TemporaryDirectory(prefix='kit-definition-') as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            require(len({i.filename for i in infos}) == len(infos), 'Duplicate packaged member')
            for info in infos:
                safe_name(info.filename.rstrip('/'))
            archive.extractall(root)
        command = [sys.executable, str(root / 'apply.py'), '--emit-definition', project]
        for source in sources:
            command += ['--rompath', str(Path(source).resolve())]
        subprocess.run(command, cwd=root, check=True)
        data = (root / 'collection.json').read_bytes()
        target = path.with_name(path.name + '.native')
        try:
            with zipfile.ZipFile(path) as original, zipfile.ZipFile(target, 'w') as updated:
                for info in original.infolist():
                    if info.filename != 'collection.json':
                        updated.writestr(info, original.read(info))
                info = zipfile.ZipInfo('collection.json', date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                updated.writestr(info, data)
            target.replace(path)
        finally:
            target.unlink(missing_ok=True)
