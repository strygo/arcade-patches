"""Refresh one native collection entry during the existing qualified-kit import.

The kit owns its definition. No adapter or separate build recipe is generated.
An interrupted import leaves a journal and blocks site generation until recovery.
"""
import hashlib
import json
import re
import zipfile
import base64
import catalog_index

from release_inventory import (candidate_release, load_site_entries, read_json, write_json_atomic, require, sha256_file)
from tool_inventory import ORIGIN, contract, local_path

JOURNAL = 'data/collection-import-transaction.json'
INDEX = 'docs/catalog/v1/index.json'
INVENTORY = 'data/tool-releases.json'


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def fingerprint(path):
    return sha256_file(path) if path.exists() else None


def pending_state(root, slug, version, archive, digest, index, record, kit_path=None):
    sidecars = {}
    if (root / INDEX).is_file() and read_json(root / INDEX).get('schema') == 2:
        def read(url):
            if url == ORIGIN + archive:
                return kit_path.read_bytes()
            return local_path(root / 'docs', url.removeprefix(ORIGIN)).read_bytes()
        index, sidecars = catalog_index.publish(index, read)
        for name, raw in sidecars.items():
            record['files']['catalog/v1/' + name] = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    missing = {}
    for name, raw in sidecars.items():
        target = local_path(root / 'docs/catalog/v1', name)
        require(not target.exists() or target.read_bytes() == raw, 'Definition collision')
        if not target.exists():missing[name] = raw
    sidecars = missing
    payload = encoded(index)
    record['generation'] = index['generation']
    record['files']['catalog/v1/index.json'] = {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    return {'schema': 1, 'project': slug, 'version': version, 'archive': archive,
        'kit_sha256': digest, 'index': index, 'inventory': record,
        'definitions': {name: base64.b64encode(raw).decode() for name, raw in sidecars.items()},
        'before': {name: fingerprint(root / name) for name in (INDEX, INVENTORY)}}


def prepare(root, ready_path, candidate):
    require(not (root / JOURNAL).exists(), 'unfinished collection import; run import_release.py --recover')
    if not (root / INVENTORY).exists():return None
    ready, release = candidate_release(ready_path, candidate)
    slug = release['plan']['kit']
    index = catalog_index.expand(read_json(root / INDEX),
        lambda url: local_path(root / 'docs', url.removeprefix(ORIGIN)).read_bytes())
    previous = index['projects'].get(slug)
    old_release = index['releases'][previous['current']] if previous else {}
    definitions = []
    for name in release['downloads']:
        path = candidate / 'downloads' / name
        with zipfile.ZipFile(path) as archive:
            if 'collection.json' in archive.namelist():
                require(len(archive.namelist()) == len(set(archive.namelist())), 'duplicate kit member')
                definition = json.loads(archive.read('collection.json'))
                manifest = json.loads(archive.read('manifest.json'))
                require(definition.get('api') == 1 and definition.get('project') == slug and
                    definition['manifest_sha256'] == hashlib.sha256(archive.read('manifest.json')).hexdigest() and
                    definition['game_version'] == manifest['version'] == release['plan']['version'] and
                    definition['kit_revision'] == manifest.get('kit_revision', 1) == release['plan'].get('kit_revision', 1),
                    'native collection definition identity differs')
                definitions.append((name, path, definition))
    require(not old_release.get('runner') or definitions, 'native kit update must retain its collection definition')
    require(definitions, 'game kit must include a kit-owned native collection definition')
    require(len(definitions) == 1, 'ambiguous native collection kit')
    record = read_json(root / INVENTORY)
    builder_kit = record.get('catalog_qualification', {}).get('native_tooling', {}).get(slug, {})
    require('native-kit-v1' in record['package_receipt'].get('features', []),
        'prepare a launcher with native-kit-v1 before importing a native collection kit')
    name, path, definition = definitions[0]
    require(set(definition.get('requires', [])) <= set(record['package_receipt'].get('features', [])),
            'prepare a launcher supporting this native kit’s required capabilities')
    qualified = {}
    declared_cases = release['qa']['cases']
    case_downloads = {case['id']: case['download'] for case in declared_cases}
    observed_cases = ready['qa']['cases']
    require(len(case_downloads) == len(declared_cases) == len(observed_cases)
            and {case['id'] for case in observed_cases} == set(case_downloads),
            'native collection QA case binding differs')
    for case in observed_cases:
        require(case.get('status') == 'passed', 'unpassed collection output QA case')
        # The definition belongs to this kit, while release QA also covers
        # separate downloads such as standalone overlay MRAs.
        if case_downloads[case['id']] != name:continue
        for output, spec in case['outputs'].items():
            require(output not in qualified or qualified[output] == spec, 'conflicting qualified outputs')
            qualified[output] = spec
    reports = definition.get('native_reports', [])
    require(isinstance(reports, list) and set(reports) <= {'files.json', 'verification.json'}, 'unknown native audit report scope')
    qualified = {name: spec for name, spec in qualified.items() if name not in reports}
    require(qualified and definition['native_outputs'] == qualified, 'native definition differs from independent release QA')
    key = slug + '-kit'
    revision = definition['kit_revision']
    if old_release.get('runner') and old_release['game_version'] == definition['game_version']:
        old_hash = index['artifacts'][old_release['runner']['artifact']]['sha256']
        baseline = release['plan'].get('tooling_baseline', {})
        # The first standalone tooling release may finalize the builder's early
        # bundle. Its independent QA is checked above, and the ordinary importer
        # still checks the exact public baseline and contiguous tooling history.
        adopting = builder_kit.get('kit_sha256') == old_hash and (
            old_hash == release['downloads'][name] or
            builder_kit.get('baseline_kit_sha256') in baseline.get('files', {}).values())
        require(revision > old_release['kit_revision'] or (revision == old_release['kit_revision'] and adopting),
                'native tooling revision must increase unless adopting its builder bundle')
    release_id = slug + '-' + re.sub('[^a-z0-9_-]', '-', definition['game_version'].lower()) + '-r' + str(revision)
    native = catalog_index.native_release(definition, key)
    record.get('catalog_qualification', {}).get('native_tooling', {}).pop(slug, None)
    for dependency, spec in definition.get('artifacts', {}).items():
        require(dependency not in index['artifacts'] or index['artifacts'][dependency] == spec,
                'native dependency ID collision')
        require(spec['url'].startswith(ORIGIN), 'native dependency must be hosted by the site')
        dependency_path = local_path(root / 'docs', spec['url'].removeprefix(ORIGIN))
        require(contract(dependency_path) == {k: spec[k] for k in ('size', 'sha256')},
                'publish the qualified native dependency before importing this kit')
        index['artifacts'][dependency] = spec
        record['files'][spec['url'].removeprefix(ORIGIN)] = contract(dependency_path)
    require(native['targets'] and native['prerequisites'], 'empty native collection definition')
    project = previous
    if not project:
        # A project's first catalog entry takes the page title; kit manifests carry no title.
        title = manifest.get('title') or load_site_entries(root).get(slug, {}).get('title')
        require(isinstance(title, str) and title, 'new catalog project needs a page title')
        project = {'title': title}
    index['projects'][slug] = {**project, 'current': release_id}
    index['releases'][release_id] = native
    if previous and previous['current'] != release_id:
        index['releases'].pop(previous['current'])
    archive_name = 'downloads/archive/' + release['downloads'][name] + '/' + name
    index['artifacts'][key] = {**contract(path), 'url': ORIGIN + archive_name}
    # Drop only the replaced kit artifact record. Immutable
    # blobs stay hosted for clients retaining an older catalog.
    old_keys = {old_release.get('runner', {}).get('artifact')}
    used = set()
    for item in index['releases'].values():
        used.add(item['runner']['artifact'])
    for artifact in old_keys - used:
        if artifact:index['artifacts'].pop(artifact, None)
    index['generation'] += 1
    update = {'project': slug, 'game_version': definition['game_version'], 'kit_revision': revision,
        'ready_sha256': sha256_file(ready_path), 'release_sha256': ready['release_sha256'],
        'kit_sha256': release['downloads'][name]}
    index.setdefault('native_updates', []).append(update)
    record.setdefault('native_updates', []).append(update)
    record['files'][archive_name] = contract(path)
    return pending_state(root, slug, definition['game_version'], archive_name,
        release['downloads'][name], index, record, path)


def begin(root, pending):
    if pending:
        require(not (root / JOURNAL).exists(), 'collection import already pending')
        write_json_atomic(root / JOURNAL, pending)


def recover(root):
    path = root / JOURNAL
    if not path.exists():return False
    pending = read_json(path)
    inventory = read_json(root / 'data/releases.json')
    project = inventory['releases'].get(pending['project'], {})
    selected = project.get('versions', {}).get(project.get('current'), {})
    imported = project.get('current') == pending['version'] and any(
        spec['sha256'] == pending['kit_sha256'] for spec in selected.get('files', {}).values())
    if imported:
        archive = local_path(root / 'docs', pending['archive'])
        require(sha256_file(archive) == pending['kit_sha256'], 'imported native kit archive changed')
        writes = [(INDEX, pending['index']), (INVENTORY, pending['inventory'])]
        for name, value in writes:
            desired = hashlib.sha256(encoded(value)).hexdigest()
            require(fingerprint(root / name) in (pending['before'][name], desired),
                'collection publication changed outside the pending transaction')
        for name, data in pending.get('definitions', {}).items():
            target = local_path(root / 'docs/catalog/v1', name)
            raw = base64.b64decode(data, validate=True)
            require(not target.exists() or target.read_bytes() == raw, 'Definition collision')
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                temporary = target.with_suffix('.json.tmp')
                temporary.write_bytes(raw);temporary.replace(target)
        for name, value in writes:
            if not (root / name).exists() or (root / name).read_bytes() != encoded(value):write_json_atomic(root / name, value)
    else:
        require(all(fingerprint(root / name) == digest for name, digest in pending['before'].items()),
            'collection changed before the kit import completed')
    path.unlink()
    return imported
