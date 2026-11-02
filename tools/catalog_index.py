"""Publish a discovery index; extract native definitions without rewriting bytes."""
import copy
import hashlib
import io
import json
import zipfile

ORIGIN = 'https://arcadepatches.com/catalog/v1/'


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def native_release(definition, kit):
    if definition.get('api') != 1:
        raise ValueError('Unknown native definition API')
    return {**{k: copy.deepcopy(definition[k]) for k in ('environments', 'helpers') if k in definition},
        'protocol': 1, 'minimum_launcher': '0.1.0',
        'requires': sorted(set(['native-kit-v1', 'file-inputs-v1', 'archive-inputs-v1', 'zip-outputs-v1', *definition.get('requires', [])])),
        'game_version': definition['game_version'], 'kit_revision': definition['kit_revision'],
        'runner': {'api': 1, 'artifact': kit, 'entry': 'apply.py'},
        'prerequisites': definition['prerequisites'], 'targets': definition['targets']}


def expand(index, read):
    if index['schema'] == 1:
        return copy.deepcopy(index)  # Internal producer model, never the published index.
    if index['schema'] != 2:
        raise ValueError('Unknown discovery index schema')
    result = copy.deepcopy(index);result['schema'] = 1
    for slug, project in result['projects'].items():
        key = project['current'];pointer = index['releases'][key]
        raw = read(pointer['definition']['url'])
        spec = pointer['definition']
        if len(raw) != spec['size'] or hashlib.sha256(raw).hexdigest() != spec['sha256']:
            raise ValueError('Definition differs: ' + slug)
        value = json.loads(raw)
        if spec['kind'] != 'native-kit-v1':
            raise ValueError('Only native kit definitions are supported')
        if any(index['artifacts'].get(key) != pin for key, pin in value.get('artifacts', {}).items()):
            raise ValueError('Native dependency differs from kit definition')
        if value['project'] != slug:
            raise ValueError('Definition project differs')
        value = native_release(value, pointer['kit'])
        if any(value[k] != pointer[k] for k in ('game_version', 'kit_revision')):
            raise ValueError('Definition revision differs')
        result['releases'][key] = value
    return result


def publish(model, read):
    """Return index and immutable sidecars. Native bytes come only from the kit."""
    result = copy.deepcopy(model);result['schema'] = 2
    result.pop('native_updates', None)  # Publication receipts belong in the inventory.
    sidecars = {}
    for slug, project in model['projects'].items():
        key = project['current'];release = model['releases'][key]
        pointer = {k: release[k] for k in ('game_version', 'kit_revision')}
        if not release.get('runner') or 'adapter' in release or 'payloads' in release:
            raise ValueError('Only native kit execution is supported')
        kit = release['runner']['artifact']
        with zipfile.ZipFile(io.BytesIO(read(model['artifacts'][kit]['url']))) as archive:
            if archive.namelist().count('collection.json') != 1:
                raise ValueError('Missing or duplicate native definition')
            raw = archive.read('collection.json')
        definition = json.loads(raw)
        expected = native_release(definition, kit)
        if definition['project'] != slug or any(expected[k] != release[k] for k in expected if k != 'requires') or set(expected['requires']) != set(release['requires']):
            raise ValueError('Native definition differs from qualified release: ' + slug)
        pointer['kit'] = kit;kind = 'native-kit-v1'
        digest = hashlib.sha256(raw).hexdigest()
        name = 'definitions/' + digest + '.json'
        sidecars[name] = raw
        pointer['definition'] = {'kind': kind, 'url': ORIGIN + name, 'size': len(raw), 'sha256': digest}
        result['releases'][key] = pointer
    return result, sidecars
