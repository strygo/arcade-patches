"""Kit-owned exact inputs and complete exports for a native CHD patch."""
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


def require(value, message):
    if not value:raise ValueError(message)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 ** 2), b''):value.update(block)
    return value.hexdigest()


def encoded(value):return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def definition(root, manifest):
    root = Path(root);native = manifest['native'];source = manifest['source'];project = manifest['project']
    key = 'disc-' + native['source_sha256'][:20]
    original = manifest['set'] + '/' + manifest['chd']
    outputs = {'roms/' + original: {'sha256': native['output_sha256']},
               'support/' + project + '/readme.txt': {'sha256': digest(root / 'readme.txt')}}
    return {'api': 1, 'project': project, 'game_version': manifest['version'], 'kit_revision': manifest['kit_revision'],
        'manifest_sha256': digest(root / 'manifest.json'), 'requires': ['native-context-v1', 'native-bindings-v1', 'target-regions-v1'],
        'prerequisites': {key: {'title': manifest['game'] + ': original ' + manifest['chd'], 'kind': 'disc',
            'formats': ['CHD', 'ZIP member', '7z member'], 'alternatives': [{'size': source['size'], 'sha256': native['source_sha256']}],
            'action': 'Add your unmodified canonical MAME GD-ROM CHD to a scanned folder. Names may differ.'}},
        'targets': {'multiregion-mame': {'platform': 'mame', 'variant': 'multiregion', 'regions': ['jp', 'us', 'world'],
            'inputs': [key], 'minimum_free_bytes': 4 * 1024 ** 3, 'outputs': outputs,
            'software_notes': ['The kit prepares its pinned chdman helper automatically. Allow about 2.5 GB of temporary space.',
                'Keep your existing arcade set and NAOMI BIOS in MAME. Japan, USA and Export share this output.']}},
        'native_outputs': {original: {'sha256': native['output_sha256']}}}


def seal(root, project, revision, source_sha256, output_sha256):
    root = Path(root);manifest = json.loads((root / 'manifest.json').read_text())
    manifest.update(api=1, project=project, kit_revision=revision, native={'source_sha256': source_sha256, 'output_sha256': output_sha256})
    manifest['files'] = {p.relative_to(root).as_posix(): digest(p) for p in sorted(root.rglob('*')) if p.is_file()
        and p.relative_to(root).as_posix() not in ('manifest.json', 'collection.json') and '__pycache__' not in p.parts}
    (root / 'manifest.json').write_bytes(encoded(manifest))
    result = definition(root, manifest);(root / 'collection.json').write_bytes(encoded(result));return result


def describe(root):
    root = Path(root);manifest = json.loads((root / 'manifest.json').read_text())
    value = json.loads((root / 'collection.json').read_text())
    require(value == definition(root, manifest), 'CHD definition differs from native manifest')
    for name, expected in manifest['files'].items():
        require(not Path(name).is_absolute() and '..' not in Path(name).parts and '\\' not in name, 'Unsafe native file path')
        require(digest(root / name) == expected, 'CHD producer changed: ' + name)
    return value


def run(root, args):
    root = Path(root);value = describe(root)
    require(args.platform == 'mame' and args.variant == 'multiregion', 'Unknown CHD export')
    bound = json.loads(args.inputs_json.read_text());context = bound['context'];bindings = bound['bindings']
    target = value['targets']['multiregion-mame']
    require(bound['schema'] == 1 and set(bindings) == set(target['inputs']), 'Incomplete or unexpected CHD bindings')
    key = target['inputs'][0];binding = bindings[key];pin = value['prerequisites'][key]['alternatives'][0]
    require(binding['size'] == pin['size'] and binding['sha256'] == pin['sha256'], 'Undeclared CHD identity')
    require(Path(context['run_cache']).is_absolute() and Path(context['tools_cache']).is_absolute(), 'Native cache paths must be absolute')
    os.environ.update(CPSPLUS_SWITCH_TOOLS_CACHE=context['tools_cache'], ARCADE_PYTHON_CACHE=str(Path(context['tools_cache']) / 'python'))
    if context['offline']:os.environ['CPSPLUS_SWITCH_TOOLS_OFFLINE'] = '1'
    else:os.environ.pop('CPSPLUS_SWITCH_TOOLS_OFFLINE', None)
    with tempfile.TemporaryDirectory(prefix='bound-chd-', dir=context['run_cache']) as temporary:
        temporary = Path(temporary);source = Path(binding['path']);require(source.is_absolute(), 'Bound CHD path must be absolute')
        before = source.stat()
        if 'member' in binding:
            image = temporary / 'source.chd'
            with zipfile.ZipFile(source) as archive:
                matches = [row for row in archive.infolist() if row.filename == binding['member'] and not row.is_dir()]
                require(len(matches) == 1 and matches[0].file_size == pin['size'], 'Bound CHD member changed')
                with archive.open(matches[0]) as stream, image.open('wb') as output:shutil.copyfileobj(stream, output, 1024 ** 2)
        else:image = source
        require(image.stat().st_size == pin['size'] and digest(image) == pin['sha256'], 'CHD changed since discovery')
        after = source.stat();require((before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns), 'CHD changed while checking')
        from fetch_optional_tools import resolve
        helper = str(resolve('chdman'))
        previous = sys.argv
        try:
            sys.argv = [str(root / 'apply.py')];producer = runpy.run_path(str(root / 'apply.py'))
        finally:sys.argv = previous
        manifest = json.loads((root / 'manifest.json').read_text())
        chd_sha, data_sha = producer['chd_hashes'](helper, str(image))
        require(chd_sha == manifest['source']['chd_sha1'] and data_sha == manifest['source']['data_sha1'], 'Original CHD content differs')
        if not args.check:
            output = Path(args.out_dir);require(not output.exists() or not any(output.iterdir()), 'CHD export must be empty')
            destination = output / 'roms' / manifest['set'] / manifest['chd'];destination.parent.mkdir(parents=True, exist_ok=True)
            producer['apply_chd'](manifest, str(image), str(destination), helper, str(temporary))
            readme = output / 'support' / value['project'] / 'readme.txt';readme.parent.mkdir(parents=True);shutil.copyfile(root / 'readme.txt', readme)
            require({p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()} == set(target['outputs']), 'CHD export extent differs')
            for name, spec in target['outputs'].items():require(digest(output / name) == spec['sha256'], 'CHD export differs from accepted contract')
    return {'api': 1, 'target': 'multiregion-mame', 'operation': 'check' if args.check else 'build', 'status': 'ok',
            **({'outputs': target['outputs']} if not args.check else {})}


def main(root):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--describe-json', action='store_true');parser.add_argument('--collection-layout', action='store_true')
    parser.add_argument('--inputs-json', type=Path);parser.add_argument('--report-json', type=Path)
    parser.add_argument('--platform');parser.add_argument('--variant');parser.add_argument('--out-dir', type=Path);parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.describe_json:print(json.dumps(describe(root), sort_keys=True));return 0
    require(args.collection_layout and args.inputs_json and args.report_json and args.out_dir, 'Use exact bound CHD inputs')
    try:result = run(root, args)
    except (ValueError, OSError, KeyError, SystemExit) as exc:
        args.report_json.write_bytes(encoded({'api': 1, 'target': 'multiregion-mame', 'operation': 'check' if args.check else 'build',
            'status': 'error', 'message': str(exc)}));return 2
    args.report_json.write_bytes(encoded(result));return 0
