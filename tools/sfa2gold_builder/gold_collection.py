"""Gold-family native inputs, disc cache, definitions and complete exports.

The reconstruction and platform exporters remain in apply.py and serve both
standalone and collection users. This module uses bundled helpers and stdlib.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

from kit_collection import (contracts, load_bindings, read_binding, report,
                            require, safe_name, sha, verify_outputs)
from kit_identity import load_config, PLATFORMS
from rom_sources import Resolver, QSOUND, publish_tree, search_paths


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 ** 2), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def describe(root):
    root = Path(root)
    value = json.loads((root / 'collection.json').read_text())
    require(value.get('api') == 1 and value['manifest_sha256'] == file_sha(root / 'manifest.json'),
            'Gold definition differs from its manifest')
    manifest = json.loads((root / 'manifest.json').read_text())
    require(manifest['edition'] == load_config(root)['edition'] == value['project'], 'Gold edition differs')
    for name, digest in manifest['files'].items():
        safe_name(name)
        require(file_sha(root / name) == digest, 'Gold producer changed: ' + name)
    return value


def decode_disc(binding, cache):
    """Decode one verified loose source; a changed/missing cache never passes."""
    require('member' not in binding, 'Supply a loose ISO or DVD CHD, outside an archive')
    source = Path(binding['path'])
    require(source.stat().st_size == binding['size'] and file_sha(source) == binding['sha256'],
            'Source disc changed after discovery')
    if binding['format'] == 'iso':
        return source
    require(binding['format'] == 'chd', 'Unknown Gold disc format')
    with source.open('rb') as stream:
        header = stream.read(124)
    require(len(header) == 124 and header[:8] == b'MComprHD' and int.from_bytes(header[12:16], 'big') == 5,
            'Gold requires a CHD v5 DVD image')
    logical_size = int.from_bytes(header[32:40], 'big')
    require(0 < logical_size <= 9 * 1024 ** 3, 'Unsupported DVD logical size')
    cache = Path(cache) / 'discs'
    cache.mkdir(parents=True, exist_ok=True)
    decoded = cache / (binding['sha256'] + '.iso')
    marker = decoded.with_suffix('.json')
    if decoded.is_file() and marker.is_file():
        pin = json.loads(marker.read_text())
        require(pin['source_sha256'] == binding['sha256'] and decoded.stat().st_size == logical_size and
                file_sha(decoded) == pin['iso_sha256'], 'Decoded disc cache changed')
        return decoded
    require(shutil.disk_usage(cache).free > logical_size + 1024 ** 3, 'Insufficient space to decode your disc')
    from fetch_optional_tools import disc_tool
    tool = Path(disc_tool('chdman'))
    with tempfile.TemporaryDirectory(prefix='decode-', dir=cache) as temporary:
        output = Path(temporary) / 'disc.iso'
        subprocess.run([str(tool), 'extractdvd', '-i', str(source), '-o', str(output)],
                       check=True, timeout=900)
        require(output.stat().st_size == logical_size, 'Decoded DVD length differs from its CHD')
        pin = {'source_sha256': binding['sha256'], 'iso_sha256': file_sha(output),
               'chdman_sha256': file_sha(tool)}
        output.replace(decoded)
        write_json(marker, pin)
    return decoded


def project_outputs(outputs, platform, firmware):
    """Project native exports and supply the device required by each runtime."""
    require(platform in PLATFORMS, 'Select one collection platform')
    result = {}
    for name, value in outputs.items():
        safe_name(name)
        if not name.startswith(platform + '/'):
            continue
        destination = ('roms/' + Path(name).name if platform in ('mame', 'hbmame', 'fbneo')
                       else name[len(platform) + 1:])
        require(destination not in result, 'Gold export collision')
        result[destination] = value
    if platform in ('mame', 'hbmame', 'mister'):
        require(firmware is not None, 'Gold runtime export requires QSound firmware')
        # MiSTer game archives embed the device; emulator search also needs a
        # qsound set. The native MRA references the embedded archive first.
        if platform != 'mister':
            result['roms/qsound.zip'] = {'dl-1425.bin': firmware}
    require(result, 'No Gold outputs for selected platform')
    return result


def read_tree(root):
    result = {}
    for path in sorted(Path(root).rglob('*')):
        if path.is_file():
            name = path.relative_to(root).as_posix()
            if name.endswith('.zip'):
                with zipfile.ZipFile(path) as archive:
                    require(len(archive.namelist()) == len(set(archive.namelist())), 'Duplicate cached ROM member')
                    result[name] = {n: archive.read(n) for n in archive.namelist()}
            else:
                result[name] = path.read_bytes()
    return result


def cached_outputs(root, region, arc, iso, source_digest, cache, build_outputs):
    identity = sha(json.dumps({'producer': file_sha(Path(root) / 'manifest.json'), 'region': region,
                              'disc': source_digest, 'roms': {n: sha(v) for n, v in arc.items()}},
                             sort_keys=True).encode())
    folder = Path(cache) / ('gold-' + identity)
    marker = folder / 'complete.json'
    raw = folder / 'raw'
    if marker.is_file():
        result = read_tree(raw)
        verify_outputs(result, json.loads(marker.read_text()))
        return result
    folder.mkdir(parents=True, exist_ok=True)
    result = build_outputs(region, arc, iso)
    publish_tree(raw, result)
    write_json(marker, contracts(result))
    return result


def run(root, args, build_outputs):
    target_id = args.region + '-' + args.platform
    operation = 'check' if args.check else 'build'
    try:
        require(args.inputs_json and args.out_dir and args.platform in PLATFORMS and not args.out,
                'Gold collection needs bound inputs, one platform and an output directory')
        value = describe(root)
        require(target_id in value['targets'], 'Unknown Gold collection target')
        target = value['targets'][target_id]
        request = json.loads(Path(args.inputs_json).read_text())
        context = request['context']
        cache = Path(context['run_cache'])
        require(cache.is_absolute() and Path(context['tools_cache']).is_absolute(), 'Native cache paths must be absolute')
        os.environ['CPSPLUS_SWITCH_TOOLS_CACHE'] = context['tools_cache']
        os.environ['ARCADE_PYTHON_CACHE'] = str(Path(context['tools_cache']) / 'python')
        if context['offline']:
            os.environ['CPSPLUS_SWITCH_TOOLS_OFFLINE'] = '1'
        else:
            os.environ.pop('CPSPLUS_SWITCH_TOOLS_OFFLINE', None)
        os.environ.pop('CAPCOM_ARCADE_ROM_PATH', None)
        bindings = load_bindings(args.inputs_json)
        disc = value['prerequisites'][target['disc']]
        matches = [(row, alt) for row in bindings for alt in disc['alternatives']
                   if row['size'] == alt['size'] and row['sha256'] == alt['sha256']]
        require(len(matches) == 1, 'Missing or ambiguous bound Gold disc')
        row, alt = matches[0]
        iso = decode_disc(dict(row, format=alt['format']), cache)
        catalog = json.loads((Path(root) / 'rom_inputs.json').read_text())['sets']
        source = catalog[load_config(root)['sets'][args.region]['4mb']]
        resolver = Resolver([], bindings=args.inputs_json)
        arc = resolver.resolve([s for s in source.values() if s['role'] == 'game'])
        need_firmware = args.platform in ('mame', 'hbmame', 'mister')
        arc.update(resolver.devices(include=need_firmware))
        if operation == 'check':
            import extract
            profile = getattr(extract, args.region.upper())
            extract.extract_zero6(str(iso), profile)
            extract.extract_audio(str(iso), profile)
            report(args.report_json, operation, target_id)
            return
        outputs = cached_outputs(root, args.region, arc, iso, row['sha256'], cache, build_outputs)
        # Compare the complete native extent as well as the selected export.
        expected = value['regions'][args.region]
        if not need_firmware:
            expected = {n: {'members': {k: h for k, h in spec['members'].items() if k != 'dl-1425.bin'}}
                        if 'members' in spec else spec for n, spec in expected.items()}
        verify_outputs(outputs, expected)
        selected = project_outputs(outputs, args.platform, arc.get('dl-1425.bin'))
        verify_outputs(selected, target['outputs'])
        publish_tree(Path(args.out_dir), selected)
        report(args.report_json, operation, target_id, outputs=contracts(selected))
    except Exception as exc:
        report(args.report_json, operation, target_id, status='failed', message=str(exc))
        raise


def define(root, version, revision, discs, rompaths, cache, build_outputs):
    """Normal packager: derive definitions from the actual native producer."""
    root = Path(root)
    kit = load_config(root)
    required_discs = {'us' if r == 'asia' else r for r in kit['sets']}
    require(set(discs) == required_discs, 'Supply exactly the discs for this edition')
    media, decoded = {}, {}
    for region, source in sorted(discs.items()):
        source = Path(source).resolve()
        with source.open('rb') as stream:
            kind = 'chd' if stream.read(8) == b'MComprHD' else 'iso'
        binding = {'path': str(source), 'size': source.stat().st_size, 'sha256': file_sha(source), 'format': kind}
        iso = decode_disc(binding, cache)
        decoded[region] = iso
        alternatives = [{k: binding[k] for k in ('size', 'sha256', 'format')}]
        if kind == 'chd':
            alternatives.append({'size': iso.stat().st_size, 'sha256': file_sha(iso), 'format': 'iso'})
        media[region] = {'alternatives': alternatives}
    write_json(root / 'media.json', media)
    producer_names = [p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and
                     (p.suffix == '.py' and 'kit_python' not in p.parts or
                      p.name in ('kit.json', 'rom_inputs.json', 'media.json', 'fbneo_names.json',
                                 'darksoft_profiles.json', 'darksoft_kits.json') or
                      p.parent.name in ('recipes', 'mras'))]
    manifest = {'title': kit['title'], 'version': version, 'kit_revision': revision, 'edition': kit['edition'],
                'files': {n: file_sha(root / n) for n in sorted(producer_names)}}
    write_json(root / 'manifest.json', manifest)
    regional = {}
    catalog = json.loads((root / 'rom_inputs.json').read_text())['sets']
    for region, names in kit['sets'].items():
        disc_region = 'us' if region == 'asia' else region
        resolver = Resolver(search_paths(rompaths=rompaths, kit=root))
        arc = resolver.resolve([s for s in catalog[names['4mb']].values() if s['role'] == 'game'])
        arc.update(resolver.devices(include=True))
        regional[region] = contracts(cached_outputs(root, region, arc, decoded[disc_region],
            media[disc_region]['alternatives'][0]['sha256'], cache, build_outputs))
    return seal_contracts(root, version, revision, media, regional)


def seal_contracts(root, version, revision, media, regional):
    """Seal measured native outputs, including tooling repacks of accepted bytes."""
    root = Path(root);kit = load_config(root)
    require(set(regional) == set(kit['sets']), 'Native output region extent differs')
    require(set(media) == {'us' if r == 'asia' else r for r in kit['sets']}, 'Native disc extent differs')
    write_json(root / 'media.json', media)
    producer_names = [p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and
                     (p.suffix == '.py' and 'kit_python' not in p.parts or
                      p.name in ('kit.json', 'rom_inputs.json', 'media.json', 'fbneo_names.json',
                                 'darksoft_profiles.json', 'darksoft_kits.json') or
                      p.parent.name in ('recipes', 'mras'))]
    manifest = {'title': kit['title'], 'version': version, 'kit_revision': revision, 'edition': kit['edition'],
                'files': {n: file_sha(root / n) for n in sorted(producer_names)}}
    write_json(root / 'manifest.json', manifest)
    catalog = json.loads((root / 'rom_inputs.json').read_text())['sets']
    value = {'api': 1, 'project': kit['edition'], 'game_version': version, 'kit_revision': revision,
             'manifest_sha256': file_sha(root / 'manifest.json'),
             'requires': ['native-context-v1', 'disc-inputs-v1', 'run-cache-v1'],
             'prerequisites': {}, 'targets': {}, 'regions': {}, 'native_outputs': {}}
    for region, names in kit['sets'].items():
        disc_region = 'us' if region == 'asia' else region
        disc_id = 'disc-' + disc_region
        title = {'us': 'Street Fighter Alpha Anthology (USA)', 'eu': 'Street Fighter Alpha Anthology (Europe)',
                 'jp': "Street Fighter Zero: Fighter's Generation (Japan)"}[disc_region]
        value['prerequisites'][disc_id] = {'title': title + ' — PlayStation 2 disc', 'kind': 'disc',
            'formats': ['verified loose ISO', 'verified CHD v5 DVD'],
            'action': 'Add your matching PS2 disc image to a media folder. Archives containing disc images must be extracted.',
            'alternatives': media[disc_region]['alternatives']}
        specs = [s for s in catalog[names['4mb']].values() if s['role'] == 'game']
        ids = []
        for spec in [*specs, QSOUND]:
            key = 'rom-' + spec['sha256'][:20]
            value['prerequisites'][key] = {'title': names['4mb'] + ': ' + spec['name'],
                'kind': 'firmware' if spec['name'] == 'dl-1425.bin' else 'rom',
                'formats': ['loose ROM', 'ZIP member', '7z member'],
                'action': 'Add the canonical ' + names['4mb'] + ' MAME set and QSound firmware to a media folder.',
                'alternatives': [{k: spec[k] for k in ('size', 'sha256', 'crc32') if k in spec}]}
            ids.append(key)
        native = regional[region]
        for name, spec in native.items():
            require(name not in value['native_outputs'], 'Native output region collision')
            value['native_outputs'][name] = spec
        value['regions'][region] = native
        for platform in PLATFORMS:
            selected = project_outputs(native, platform, QSOUND['sha256'])
            if platform in ('mame', 'hbmame'):
                selected['roms/qsound.zip'] = {'members': selected['roms/qsound.zip']}
            value['targets'][region + '-' + platform] = {
                'platform': platform, 'variant': region, 'native_variant': region, 'region': region,
                'profile': kit['edition'] + '-' + region if platform == 'mame' else 'default',
                'disc': disc_id, 'inputs': sorted([disc_id, *(ids if platform in ('mame', 'hbmame', 'mister') else ids[:-1])]),
                'outputs': selected, 'minimum_free_bytes': 12 * 1024 ** 3,
                'destination_requirements': ('Use emulator definitions matching this release.' if platform in ('mame', 'hbmame', 'fbneo') else
                    'Use the core named by the included MRA.' if platform == 'mister' else 'Use the Darksoft layout in this kit.'),
                'software_notes': ['DVD CHDs use an installed chdman or the pinned helper fetched by this kit.']}
    write_json(root / 'collection.json', value)
    return value


def package_main(root, argv, build_outputs):
    parser = argparse.ArgumentParser(description='Generate this kit’s collection definition')
    parser.add_argument('--emit-definition', required=True)
    parser.add_argument('--game-version', required=True)
    parser.add_argument('--kit-revision', type=int, required=True)
    parser.add_argument('--disc', action='append', default=[])
    parser.add_argument('--rompath', action='append', default=[])
    parser.add_argument('--run-cache', type=Path, required=True)
    args = parser.parse_args(argv)
    require(args.emit_definition == load_config(root)['edition'], 'Wrong collection edition')
    discs = dict(item.split('=', 1) for item in args.disc)
    require(len(discs) == len(args.disc), 'Duplicate disc region')
    define(root, args.game_version, args.kit_revision, discs, args.rompath, args.run_cache, build_outputs)
