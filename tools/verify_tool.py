#!/usr/bin/env python3
"""Verify the tool publication and optional HTTP download closure, without builds."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import quote
from tool_inventory import validate, check_file, ORIGIN, local_path
import catalog_index
from verify_hosted_releases import hosted_digest


def verify(root, base_url=None):
    entry = json.loads((root / 'data/tools.json').read_text())
    record = validate(root, entry)
    if record is None:
        raise ValueError('Tool is still hidden')
    checked = {}
    if base_url:
        for name, spec in sorted(record['files'].items()):
            size, digest = hosted_digest(base_url.rstrip('/') + '/' + quote(name))
            if (size, digest) != (spec['size'], spec['sha256']):
                raise ValueError('Hosted tool dependency differs: ' + name)
            checked[name] = spec
    index = json.loads((root / 'docs/catalog/v1/index.json').read_text())
    index = catalog_index.expand(index, lambda url: local_path(root / 'docs', url.removeprefix(ORIGIN)).read_bytes())
    games = json.loads((root / 'data/releases.json').read_text())
    native = {}
    for slug, project in index['projects'].items():
        release = index['releases'][project['current']]
        if not release.get('runner') or 'adapter' in release or 'payloads' in release:
            raise ValueError('Only native kit execution is supported')
        publication = games['releases'][slug]
        selected = publication['versions'][publication['current']]
        if release['game_version'] != publication['current']:
            raise ValueError('Native catalog differs from selected game release: ' + slug)
        artifact = index['artifacts'][release['runner']['artifact']]
        role = {'rom': 'ips', 'kit': 'kit', 'chd': 'chd'}[selected['kind']]
        if artifact['sha256'] != selected['files'][role]['sha256']:
            # The builder can ship a native tooling bundle ahead of the game's
            # standalone tooling release. Its accepted game baseline stays pinned.
            proof = record['catalog_qualification'].get('native_tooling', {}).get(slug, {})
            if not (proof.get('game_version') == publication['current'] and
                    proof.get('kit_revision') == release['kit_revision'] and
                    proof.get('baseline_kit_sha256') == selected['files'][role]['sha256'] and
                    proof.get('kit_sha256') == artifact['sha256'] and
                    proof.get('ready_record_sha256') == selected['qualification']['ready_record_sha256'] and
                    proof.get('accepted_export_contracts') == 'unchanged' and proof.get('isolated_metadata') == 'passed'):
                raise ValueError('Native builder kit lacks its accepted game binding: ' + slug)
        elif release['kit_revision'] != selected.get('current_revision', 1):
            raise ValueError('Native tooling revision differs from selected game release: ' + slug)
        page = (root / 'docs' / slug / 'index.html').read_text()
        for spec in selected['files'].values():
            name = 'downloads/' + spec['name']
            check_file(root / 'docs' / name, spec)
            if spec['sha256'] not in page or ('../' + name) not in page:
                raise ValueError('Native game page download differs: ' + slug)
            native[name] = {k:spec[k] for k in ('size','sha256')}
            if base_url:
                size, digest = hosted_digest(base_url.rstrip('/') + '/' + quote(name))
                if (size,digest) != (spec['size'],spec['sha256']):raise ValueError('Hosted native game alias differs: ' + name)
                checked[name] = native[name]
    return {'schema': 1, 'status': 'passed', 'version': entry['version'],
            'scope': 'tool inventory and HTTP catalog/launcher/dependency closure; no game reconstruction or runtime gates',
            'generation': record['generation'], 'publication_inventory_sha256': hashlib.sha256(
                (root / 'data/tool-releases.json').read_bytes()).hexdigest(),
            'local_files': len(record['files']) + len(native), 'native_game_aliases': native, 'http_base': base_url, 'hosted_files': checked}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', help='Site root to check over HTTP; omit for local files only')
    parser.add_argument('--record', type=Path)
    args = parser.parse_args()
    if args.record and args.record.exists():
        parser.error('Choose a fresh record path')
    result = verify(Path(__file__).resolve().parents[1], args.base_url)
    if args.record:
        args.record.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print(f"Passed: {result['local_files']} tool publication files; {len(result['hosted_files'])} HTTP files")
