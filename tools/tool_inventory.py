"""Import portable tools and their complete, hash-pinned download catalogs."""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
import shutil
from urllib.parse import urlsplit
import zipfile
import catalog_index

ORIGIN = 'https://arcadepatches.com/'
LAUNCHER_PAGE = ORIGIN + 'arcadepatches/'


def version_tuple(value):
    require(isinstance(value, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value), 'Launcher versions use major.minor.patch')
    return tuple(int(part) for part in value.split('.'))


def display_version(version):
    """The page and ZIP name of a launcher version: a .0 patch keeps its short name (0.1.0 is 0.1)."""
    version_tuple(version)
    return version.removesuffix('.0')


def digest(path):
    with Path(path).open('rb') as source:
        value = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
        return value.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def local_path(root, name):
    path = PurePosixPath(name)
    require(isinstance(name, str) and path.as_posix() == name and not path.is_absolute()
            and '..' not in path.parts and '\\' not in name and name != '.', 'Unsafe publication path')
    result = root / name
    require(result.resolve().is_relative_to(root.resolve()) and not result.is_symlink(), 'Escaping publication path')
    return result


def contract(path):
    return {'size': path.stat().st_size, 'sha256': digest(path)}


def check_file(path, spec):
    require(path.is_file() and contract(path) == {k: spec[k] for k in ('size', 'sha256')},
            f'Publication file missing or changed: {path.name}')


def validate(root, entry):
    require(not (root / 'data/collection-import-transaction.json').exists(),
            'Unfinished collection import; run import_release.py --recover')
    require(entry['slug'] == 'arcadepatches' and isinstance(entry.get('version'), str)
            and re.fullmatch(r'[0-9]+\.[0-9]+(\.[0-9]+)?', entry['version']), 'Unknown tool release')
    for field in ('title', 'summary', 'date'):
        require(isinstance(entry.get(field), str) and entry[field], f'Tool page missing {field}')
    for field in ('requirements', 'features', 'steps'):
        require(isinstance(entry.get(field), list) and entry[field] and
                all(isinstance(item, str) and item for item in entry[field]), f'Tool page missing {field}')
    require(isinstance(entry.get('screenshots'), list) and entry['screenshots'], 'Tool page missing screenshots')
    for shot in entry['screenshots']:
        require(shot.get('caption') and local_path(root, shot['single']).is_file(), 'Tool screenshot missing')
    if entry.get('hidden'):
        return None
    record = json.loads((root / 'data/tool-releases.json').read_text())
    require(record['version'] == entry['version'] and record['schema'] == 1, 'Tool inventory version differs')
    require(record.get('launcher') in record['files'] and record['files'][record['launcher']] ==
            {k: record['package_receipt'][k] for k in ('size', 'sha256')}, 'Launcher publication differs from package receipt')
    docs = root / 'docs'
    for name, spec in record['files'].items():
        check_file(local_path(docs, name), spec)
    require(display_version(record['package_receipt']['version']) == entry['version'], 'Launcher package version differs')
    index = json.loads((docs / 'catalog/v1/index.json').read_text())
    require(index['generation'] == record['generation'], 'Catalog generation differs')
    # Launchers from 0.1.1 announce a newer download; the field names the published one.
    require('launcher' not in index or index['launcher'] == {'version': record['package_receipt']['version'], 'url': LAUNCHER_PAGE},
            'Catalog launcher notice differs from the published launcher')
    require(all(r.get('definition', {}).get('kind') == 'native-kit-v1' for r in index['releases'].values()),
            'Tool catalog must contain native kit definitions only')
    for artifact in [*index['artifacts'].values(),
                     *(r['definition'] for r in index['releases'].values() if 'definition' in r)]:
        parsed = urlsplit(artifact['url'])
        require(artifact['url'].startswith(ORIGIN) and not parsed.query and not parsed.fragment,
                'Catalog dependency must be hosted on arcadepatches.com')
        name = parsed.path.removeprefix('/')
        require(record['files'].get(name) == {k: artifact[k] for k in ('size', 'sha256')},
                'Catalog dependency absent from publication inventory')
    return record


def checked_package(archive, receipt_path, version=None):
    """The passed package receipt and exactly the ZIP it describes."""
    receipt = json.loads(receipt_path.read_text())
    require(receipt.get('status') == 'passed' and receipt.get('standalone_doctor') == 'passed'
            and (version is None or receipt.get('version') == version), 'Launcher package receipt is not passed ' + str(version))
    check_file(archive, receipt)
    require(archive.name == receipt['archive'] == f"ArcadePatches-{display_version(receipt['version'])}.zip",
            'Unexpected launcher filename')
    with zipfile.ZipFile(archive) as source:
        require(set(source.namelist()) == set(receipt['files']) == {'arcadepatches.py', 'QUICKSTART.txt'}
                and len(source.namelist()) == 2, 'Unexpected launcher ZIP members')
        for name, spec in receipt['files'].items():
            data = source.read(name)
            require({'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} == spec, 'Launcher contents differ')
    return receipt


def write_bytes_once(target, source):
    if target.exists():
        check_file(target, contract(source))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def import_launcher(root, archive, receipt_path, date):
    """Publish a newer launcher over the current catalog: its download, the catalog notice and the tool page version."""
    require(not (root / 'data/collection-import-transaction.json').exists(),
            'Unfinished collection import; run import_release.py --recover')
    require(isinstance(date, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', date), 'Release date uses YYYY-MM-DD')
    entry = json.loads((root / 'data/tools.json').read_text())
    previous = json.loads((root / 'data/tool-releases.json').read_text())
    validate(root, entry)
    receipt = checked_package(archive, receipt_path)
    old = previous['package_receipt']
    require(version_tuple(receipt['version']) > version_tuple(old['version']), 'Launcher version must increase')
    require(set(old['features']) <= set(receipt['features']), 'A launcher update cannot drop catalog features')
    launcher = 'tools/downloads/archive/' + receipt['sha256'] + '/' + archive.name
    index_path = root / 'docs/catalog/v1/index.json'
    index = json.loads(index_path.read_text())
    index['launcher'] = {'version': receipt['version'], 'url': LAUNCHER_PAGE}
    index['generation'] += 1  # Launchers reject a changed catalog without a new generation.
    payload = (json.dumps(index, indent=2, sort_keys=True) + '\n').encode()
    # Every check is done; publish the immutable blob before moving the pointers.
    write_bytes_once(local_path(root / 'docs', launcher), archive)
    record = dict(previous, version=display_version(receipt['version']), generation=index['generation'],
                  launcher=launcher, package_receipt=receipt)
    record['files'] = dict(previous['files'])
    record['files'][launcher] = contract(archive)
    record['files']['catalog/v1/index.json'] = {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    index_path.with_suffix('.json.tmp').write_bytes(payload)
    index_path.with_suffix('.json.tmp').replace(index_path)
    (root / 'data/tool-releases.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    entry.update(version=record['version'], date=date)
    (root / 'data/tools.json').write_text(json.dumps(entry, indent=2) + '\n')
    validate(root, entry)
    return record


def import_tool(root, archive, receipt_path, catalog_dir, qualification_path):
    """Preflight everything before adding immutable blobs and moving the catalog pointer."""
    entry = json.loads((root / 'data/tools.json').read_text())
    receipt = checked_package(archive, receipt_path)
    require(display_version(receipt['version']) == entry['version'], 'Launcher package is not the tool page version')
    catalog = json.loads((catalog_dir / 'index.json').read_text())
    require(catalog['schema'] == 1 and catalog['projects'] and catalog['releases'], 'Empty or unknown catalog')
    qualification = json.loads(qualification_path.read_text())
    identity = hashlib.sha256(json.dumps(catalog, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    require(qualification.get('status') == 'passed' and qualification.get('catalog_sha256') == identity
            and qualification.get('site_commit') == catalog['provenance']['site_commit']
            and qualification.get('sources', {}).get('arcadepatches.py') == receipt['files']['arcadepatches.py']['sha256'],
            'Catalog qualification does not match the catalog and launcher')
    previous = root / 'data/tool-releases.json'
    if previous.exists():
        require(catalog['generation'] > json.loads(previous.read_text())['generation'], 'Catalog generation must increase')
    known = {}
    game_inventory = json.loads((root / 'data/releases.json').read_text())
    for project in game_inventory['releases'].values():
        for version in project['versions'].values():
            for spec in version['files'].values():
                known[(spec['sha256'], spec['size'])] = 'downloads/' + spec['archive']
    files, copies = {}, {}
    launcher = 'tools/downloads/archive/' + receipt['sha256'] + '/' + archive.name
    files[launcher] = contract(archive)
    copies[launcher] = archive
    for spec in catalog['artifacts'].values():
        key = (spec['sha256'], spec['size'])
        name = known.get(key)
        if name:
            check_file(local_path(root / 'docs', name), spec)
        else:
            url = spec['url']
            prefix = ORIGIN + 'catalog/v1/'
            require(url.startswith(prefix + 'artifacts/'), 'Unexpected catalog artifact URL')
            relative = url[len(prefix):]
            source = local_path(catalog_dir, relative)
            check_file(source, spec)
            require(Path(relative).name.startswith(spec['sha256'] + '-'), 'Artifact filename must pin content')
            name = 'catalog/v1/' + relative
            copies[name] = source
        spec['url'] = ORIGIN + name
        files[name] = {k: spec[k] for k in ('size', 'sha256')}
    def read_published(url):
        name = url.removeprefix(ORIGIN)
        source = copies.get(name) or local_path(root / 'docs', name)
        return source.read_bytes()
    catalog, sidecars = catalog_index.publish(catalog, read_published)
    if version_tuple(receipt['version']) > (0, 1, 0):
        catalog['launcher'] = {'version': receipt['version'], 'url': LAUNCHER_PAGE}
    for relative, raw in sidecars.items():
        source = local_path(catalog_dir, relative)
        source.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            require(source.read_bytes() == raw, 'Definition collision')
        else:
            source.write_bytes(raw)
        name = 'catalog/v1/' + relative
        copies[name] = source;files[name] = contract(source)
    payload = (json.dumps(catalog, indent=2, sort_keys=True) + '\n').encode()
    files['catalog/v1/index.json'] = {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    for name, source in copies.items():
        target = local_path(root / 'docs', name)
        if target.exists():
            check_file(target, files[name])
    # All source dependencies and destination conflicts have now been checked.
    for name, source in copies.items():
        target = local_path(root / 'docs', name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
    index = root / 'docs/catalog/v1/index.json'
    index.with_suffix('.json.tmp').write_bytes(payload)
    index.with_suffix('.json.tmp').replace(index)
    record = {'schema': 1, 'version': entry['version'], 'generation': catalog['generation'],
              'launcher': launcher, 'provenance': catalog['provenance'], 'package_receipt': receipt, 'catalog_qualification': qualification, 'files': files}
    previous.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    entry.pop('hidden', None)
    (root / 'data/tools.json').write_text(json.dumps(entry, indent=2) + '\n')
    validate(root, entry)
    return record
