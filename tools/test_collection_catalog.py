"""One-project catalog promotion, qualification binding and interruption recovery."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import collection_catalog as catalog
from tool_inventory import contract
from release_inventory import ReleaseInventoryError


class CatalogImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'data').mkdir();(self.root / 'docs/catalog/v1').mkdir(parents=True)
        self.candidate = self.root / 'candidate';(self.candidate / 'downloads').mkdir(parents=True)
        self.kit = self.candidate / 'downloads/game-rc2-ips.zip'
        self.native_outputs = {'fbneo/game.zip': {'members': {'p.bin': 'a' * 64}}}
        manifest = {'title': 'Game', 'version': 'rc2', 'kit_revision': 1}
        raw = catalog.encoded(manifest)
        declaration = {'api': 1, 'project': 'game', 'manifest_sha256': catalog.hashlib.sha256(raw).hexdigest(),
            'game_version': 'rc2', 'kit_revision': 1, 'native_outputs': self.native_outputs,
            'prerequisites': {'rom': {'title': 'ROM'}},
            'targets': {'default-fbneo': {'platform': 'fbneo', 'outputs': {'roms/game.zip': self.native_outputs['fbneo/game.zip']}}}}
        with zipfile.ZipFile(self.kit, 'w') as out:
            out.writestr('manifest.json', raw);out.writestr('collection.json', json.dumps(declaration))
        self.release = {'plan': {'kit': 'game', 'version': 'rc2'}, 'downloads': {self.kit.name: contract(self.kit)['sha256']}}
        self.ready = {'release_sha256': 'b' * 64, 'qa': {'cases': [{'status': 'passed', 'outputs': self.native_outputs}]}}
        self.ready_path = self.root / 'ready.json';self.ready_path.write_text('{}')
        artifacts = {};releases = {}
        for slug in ('game', 'other'):
            definition = {'api': 1, 'project': slug, 'game_version': 'rc1', 'kit_revision': 1,
                          'prerequisites': {'rom': {}}, 'targets': {'default': {'outputs': {'file': {}}}}}
            path = self.root / 'docs/catalog/v1' / (slug + '-old-kit.zip')
            with zipfile.ZipFile(path, 'w') as z:z.writestr('collection.json', json.dumps(definition))
            key = slug + '-old-kit';artifacts[key] = {**contract(path), 'url': catalog.ORIGIN + 'catalog/v1/' + path.name}
            releases[slug + '-rc1'] = catalog.catalog_index.native_release(definition, key)
        self.index = {'schema': 1, 'generation': 1, 'projects': {
            'game': {'title': 'Game', 'current': 'game-rc1'}, 'other': {'title': 'Other', 'current': 'other-rc1'}},
            'releases': releases, 'artifacts': artifacts}
        self.write(catalog.INDEX, self.index)
        self.record = {'generation': 1, 'package_receipt': {'features': ['native-kit-v1']}, 'files': {}}
        self.write(catalog.INVENTORY, self.record)
        self.write('data/releases.json', {'releases': {'game': {'current': 'rc1', 'versions': {}}}})
        self.mock = patch.object(catalog, 'candidate_release', return_value=(self.ready, self.release))
        self.mock.start();self.addCleanup(self.mock.stop)

    def write(self, name, value):
        (self.root / name).write_bytes(catalog.encoded(value))

    def prepare(self):return catalog.prepare(self.root, self.ready_path, self.candidate)

    def mark_imported(self, pending):
        dest = self.root / 'docs' / pending['archive'];dest.parent.mkdir(parents=True)
        dest.write_bytes(self.kit.read_bytes())
        self.write('data/releases.json', {'releases': {'game': {'current': 'rc2', 'versions': {
            'rc2': {'files': {'ips': contract(self.kit)}}}}}})

    def test_one_release_updates_and_removes_only_its_adapter_reference(self):
        pending = self.prepare();catalog.begin(self.root, pending);self.mark_imported(pending)
        self.assertTrue(catalog.recover(self.root))
        index = catalog.read_json(self.root / catalog.INDEX)
        self.assertEqual(index['generation'], 2)
        self.assertEqual(index['releases']['other-rc1'], self.index['releases']['other-rc1'])
        self.assertEqual(index['artifacts']['other-old-kit'], self.index['artifacts']['other-old-kit'])
        self.assertNotIn('game-old-kit', index['artifacts'])
        self.assertEqual(index['releases']['game-rc2-r1']['runner']['artifact'], 'game-kit')
        self.assertFalse((self.root / catalog.JOURNAL).exists())

    def test_first_import_takes_the_page_title(self):
        manifest = {'version': 'rc2', 'kit_revision': 1}
        raw = catalog.encoded(manifest)
        with zipfile.ZipFile(self.kit) as old:
            declaration = json.loads(old.read('collection.json'))
        declaration['manifest_sha256'] = catalog.hashlib.sha256(raw).hexdigest()
        with zipfile.ZipFile(self.kit, 'w') as kit:
            kit.writestr('manifest.json', raw);kit.writestr('collection.json', json.dumps(declaration))
        self.release['downloads'][self.kit.name] = contract(self.kit)['sha256']
        del self.index['projects']['game'];self.index['releases'].pop('game-rc1')
        self.index['artifacts'].pop('game-old-kit');self.write(catalog.INDEX, self.index)
        self.write('data/patches.json', {'patches': [{'slug': 'game', 'title': 'Game Page'}]})
        pending = self.prepare()
        self.assertEqual(pending['index']['projects']['game'], {'title': 'Game Page', 'current': 'game-rc2-r1'})
        self.write('data/patches.json', {'patches': [{'slug': 'other', 'title': 'Other'}]})
        with self.assertRaises(ReleaseInventoryError):
            self.prepare()

    def test_thin_index_import_recovers_sidecars_before_pointer(self):
        for release in self.index['releases'].values():
            release.update(game_version='rc1', kit_revision=1)
        thin, files = catalog.catalog_index.publish(self.index, lambda url: (self.root / 'docs' / url.removeprefix(catalog.ORIGIN)).read_bytes())
        for name, raw in files.items():
            target = self.root / 'docs/catalog/v1' / name
            target.parent.mkdir(parents=True, exist_ok=True);target.write_bytes(raw)
        self.write(catalog.INDEX, thin)
        pending = self.prepare()
        self.assertEqual(pending['index']['schema'], 2)
        pointer = pending['index']['releases']['game-rc2-r1']
        self.assertNotIn('targets', pointer)
        catalog.begin(self.root, pending);self.mark_imported(pending)
        self.assertTrue(catalog.recover(self.root))
        expanded = catalog.catalog_index.expand(catalog.read_json(self.root / catalog.INDEX),
            lambda url: (self.root / 'docs' / url.removeprefix(catalog.ORIGIN)).read_bytes())
        self.assertEqual(expanded['releases']['game-rc2-r1']['targets']['default-fbneo']['platform'], 'fbneo')
        with zipfile.ZipFile(self.kit) as archive:
            expected = archive.read('collection.json')
        published = self.root / 'docs' / pointer['definition']['url'].removeprefix(catalog.ORIGIN)
        self.assertEqual(published.read_bytes(), expected)
        self.assertEqual(expanded['releases']['other-rc1'], self.index['releases']['other-rc1'])

    def test_native_audit_report_scope_cannot_hide_game_outputs(self):
        with zipfile.ZipFile(self.kit) as source:
            manifest = source.read('manifest.json');definition = json.loads(source.read('collection.json'))
        definition['native_reports'] = ['fbneo/game.zip']
        with zipfile.ZipFile(self.kit, 'w') as target:
            target.writestr('manifest.json', manifest);target.writestr('collection.json', json.dumps(definition))
        self.release['downloads'] = {self.kit.name: contract(self.kit)['sha256']}
        with self.assertRaisesRegex(ReleaseInventoryError, 'audit report scope'):self.prepare()

    def test_variable_audit_report_digests_are_independent_of_game_contracts(self):
        with zipfile.ZipFile(self.kit) as source:
            manifest = source.read('manifest.json');definition = json.loads(source.read('collection.json'))
        definition['native_reports'] = ['files.json']
        with zipfile.ZipFile(self.kit, 'w') as target:
            target.writestr('manifest.json', manifest);target.writestr('collection.json', json.dumps(definition))
        self.release['downloads'] = {self.kit.name: contract(self.kit)['sha256']}
        self.ready['qa']['cases'][0]['outputs']['files.json'] = {'sha256': 'd' * 64}
        pending = self.prepare()
        self.assertEqual(pending['index']['releases']['game-rc2-r1']['targets']['default-fbneo']['outputs'],
                         {'roms/game.zip': self.native_outputs['fbneo/game.zip']})

    def test_failed_game_import_keeps_catalog_unchanged(self):
        pending = self.prepare();catalog.begin(self.root, pending)
        self.assertFalse(catalog.recover(self.root))
        self.assertEqual(catalog.read_json(self.root / catalog.INDEX), self.index)

    def test_later_standalone_import_adopts_identical_native_builder_kit(self):
        with zipfile.ZipFile(self.kit) as source:definition = json.loads(source.read('collection.json'))
        key = 'game-kit';index = self.index
        index['projects']['game']['current'] = 'game-rc2-r1'
        index['releases'].pop('game-rc1')
        index['releases']['game-rc2-r1'] = catalog.catalog_index.native_release(definition, key)
        index['artifacts'][key] = contract(self.kit)
        self.write(catalog.INDEX, index)
        self.record['catalog_qualification'] = {'native_tooling': {'game': {'kit_sha256': contract(self.kit)['sha256']}}}
        self.write(catalog.INVENTORY, self.record)
        pending = self.prepare()
        self.assertEqual(pending['index']['releases']['game-rc2-r1']['runner']['artifact'], key)
        self.assertNotIn('game', pending['inventory']['catalog_qualification']['native_tooling'])
        # A qualified standalone respin can finalize that early revision when it
        # still binds the exact accepted game baseline; the proof is consumed once.
        self.record['catalog_qualification']['native_tooling']['game']['baseline_kit_sha256'] = 'e' * 64
        self.release['plan']['tooling_baseline'] = {'files': {'kit': 'e' * 64}}
        self.record['catalog_qualification']['native_tooling']['game']['kit_sha256'] = 'd' * 64
        index['artifacts'][key]['sha256'] = 'd' * 64;self.write(catalog.INDEX, index)
        self.write(catalog.INVENTORY, self.record)
        pending = self.prepare()
        self.assertNotIn('game', pending['inventory']['catalog_qualification']['native_tooling'])
        self.record['catalog_qualification']['native_tooling']['game']['kit_sha256'] = 'f' * 64
        self.write(catalog.INVENTORY, self.record)
        with self.assertRaisesRegex(ReleaseInventoryError, 'revision must increase'):self.prepare()

    def test_new_game_requires_its_own_native_definition(self):
        self.index['projects'].pop('game');self.index['releases'].pop('game-rc1')
        self.write(catalog.INDEX, self.index)
        with zipfile.ZipFile(self.kit, 'w') as target:target.writestr('apply.py', 'print("standalone")')
        self.release['downloads'] = {self.kit.name: contract(self.kit)['sha256']}
        with self.assertRaisesRegex(ReleaseInventoryError, 'kit-owned native'):self.prepare()

    def test_native_dependencies_survive_import_and_require_exact_hosted_bytes(self):
        dependency = self.root / 'docs/catalog/v1/artifacts/native.whl'
        dependency.parent.mkdir(parents=True);dependency.write_bytes(b'QUALIFIED WHEEL')
        pin = {**contract(dependency), 'url': catalog.ORIGIN + 'catalog/v1/artifacts/native.whl', 'filename': 'native.whl'}
        with zipfile.ZipFile(self.kit) as source:
            manifest = source.read('manifest.json');definition = json.loads(source.read('collection.json'))
        definition.update(requires=['native-dependencies-v1'], artifacts={'wheel': pin},
            environments={'darwin-arm64': {'python': [3, 12], 'wheels': ['wheel']}},
            helpers={'darwin-arm64': {'helper': {'artifact': 'wheel', 'entry': 'helper'}}})
        self.record['package_receipt']['features'].append('native-dependencies-v1');self.write(catalog.INVENTORY, self.record)
        with zipfile.ZipFile(self.kit, 'w') as target:
            target.writestr('manifest.json', manifest);target.writestr('collection.json', json.dumps(definition))
        self.release['downloads'] = {self.kit.name: contract(self.kit)['sha256']}
        pending = self.prepare()
        native = pending['index']['releases']['game-rc2-r1']
        self.assertEqual(native['environments'], definition['environments'])
        self.assertEqual(native['helpers'], definition['helpers'])
        self.assertEqual(pending['index']['artifacts']['wheel'], pin)
        self.assertIn('catalog/v1/artifacts/native.whl', pending['inventory']['files'])
        dependency.write_bytes(b'CHANGED WHEEL')
        with self.assertRaisesRegex(ReleaseInventoryError, 'qualified native dependency'):self.prepare()

    def test_native_capabilities_require_matching_launcher_and_survive_import(self):
        with zipfile.ZipFile(self.kit) as source:
            manifest = source.read('manifest.json')
            definition = json.loads(source.read('collection.json'))
        definition['requires'] = ['native-context-v1', 'disc-inputs-v1', 'run-cache-v1']
        with zipfile.ZipFile(self.kit, 'w') as target:
            target.writestr('manifest.json', manifest)
            target.writestr('collection.json', json.dumps(definition))
        self.release['downloads'] = {self.kit.name: contract(self.kit)['sha256']}
        with self.assertRaisesRegex(ReleaseInventoryError, 'capabilities'):
            self.prepare()
        self.record['package_receipt']['features'].extend(definition['requires'])
        self.write(catalog.INVENTORY, self.record)
        pending = self.prepare()
        native = pending['index']['releases']['game-rc2-r1']
        self.assertTrue(set(definition['requires']) <= set(native['requires']))
        self.assertNotIn('adapter', native)

    def test_interruption_between_catalog_and_inventory_is_recoverable(self):
        pending = self.prepare();catalog.begin(self.root, pending);self.mark_imported(pending)
        self.write(catalog.INDEX, pending['index'])
        self.assertTrue(catalog.recover(self.root))
        self.assertEqual(catalog.read_json(self.root / catalog.INVENTORY), pending['inventory'])

    def test_outside_edits_are_not_overwritten_by_recovery(self):
        pending = self.prepare();catalog.begin(self.root, pending);self.mark_imported(pending)
        self.write(catalog.INDEX, {'outside': 'edit'})
        with self.assertRaisesRegex(ReleaseInventoryError, 'outside'):catalog.recover(self.root)
        self.assertEqual(catalog.read_json(self.root / catalog.INDEX), {'outside': 'edit'})

    def test_old_launcher_is_rejected_before_import(self):
        self.record['package_receipt']['features'] = [];self.write(catalog.INVENTORY, self.record)
        with self.assertRaisesRegex(ReleaseInventoryError, 'prepare a launcher'):self.prepare()
        self.assertFalse((self.root / catalog.JOURNAL).exists())

    def test_candidate_contract_must_match_independent_qa(self):
        self.ready['qa']['cases'][0]['outputs'] = {'fbneo/game.zip': {'members': {'p.bin': '0' * 64}}}
        with self.assertRaisesRegex(ReleaseInventoryError, 'independent release QA'):self.prepare()

    def test_native_update_cannot_silently_drop_its_definition(self):
        self.index['releases']['game-rc1'] = {'runner': {'artifact': 'game-kit'}}
        self.write(catalog.INDEX, self.index)
        with zipfile.ZipFile(self.kit, 'w') as out:out.writestr('manifest.json', '{}')
        with self.assertRaisesRegex(ReleaseInventoryError, 'retain its collection definition'):self.prepare()

    def promote_native_revision(self):
        # One game version, two tool revisions, with independently stored old bytes.
        legacy = self.root / 'docs/downloads/archive/legacy.zip'
        legacy.parent.mkdir(parents=True);legacy.write_bytes(b'accepted legacy kit')
        self.legacy = dict(contract(legacy), name='game-rc1-ips.zip', archive='archive/legacy.zip')
        self.index['artifacts']['game-kit'] = {**contract(legacy), 'url': catalog.ORIGIN + 'downloads/archive/legacy.zip'}
        self.index['releases']['game-rc1'].update(game_version='rc1', kit_revision=1)
        self.write(catalog.INDEX, self.index)
        self.write('data/releases.json', {'releases': {'game': {'current': 'rc1', 'versions': {
            'rc1': {'current_revision': 1, 'files': {'ips': self.legacy}, 'revisions': {'1': {'files': {'ips': self.legacy}}}}}}}})
        with zipfile.ZipFile(self.kit) as archive:
            manifest = json.loads(archive.read('manifest.json'));definition = json.loads(archive.read('collection.json'))
        manifest.update(version='rc1', kit_revision=2)
        raw = catalog.encoded(manifest)
        definition.update(game_version='rc1', kit_revision=2, manifest_sha256=catalog.hashlib.sha256(raw).hexdigest())
        with zipfile.ZipFile(self.kit, 'w') as out:
            out.writestr('manifest.json', raw);out.writestr('collection.json', json.dumps(definition))
        self.release['plan'].update(version='rc1', kit_revision=2)
        self.release['downloads'] = {self.kit.name: contract(self.kit)['sha256']}
        pending = self.prepare();catalog.begin(self.root, pending)
        dest = self.root / 'docs' / pending['archive'];dest.parent.mkdir(parents=True);dest.write_bytes(self.kit.read_bytes())
        self.native = dict(contract(self.kit), name=self.kit.name, archive=pending['archive'].removeprefix('downloads/'))
        self.publication = {'releases': {'game': {'current': 'rc1', 'versions': {'rc1': {
            'current_revision': 2, 'files': {'ips': self.native}, 'revisions': {
                '1': {'files': {'ips': self.legacy}}, '2': {'files': {'ips': self.native}}}}}}}}
        self.write('data/releases.json', self.publication)
        self.assertTrue(catalog.recover(self.root))

    def test_import_retains_no_revision_selection_snapshots(self):
        self.promote_native_revision()
        self.assertFalse((self.root / 'data/collection-history.json').exists())
        self.assertFalse((self.root / 'docs/catalog/v1/history').exists())
        self.assertFalse(any('/history/' in name for name in catalog.read_json(self.root / catalog.INVENTORY)['files']))

    def test_native_revision_moves_forward_and_preserves_other_projects(self):
        self.promote_native_revision()
        with zipfile.ZipFile(self.kit) as archive:
            manifest = json.loads(archive.read('manifest.json'));definition = json.loads(archive.read('collection.json'))
        manifest['kit_revision'] = 3
        raw = catalog.encoded(manifest)
        definition.update(kit_revision=3, manifest_sha256=catalog.hashlib.sha256(raw).hexdigest())
        with zipfile.ZipFile(self.kit, 'w') as out:
            out.writestr('manifest.json', raw);out.writestr('collection.json', json.dumps(definition))
        self.release['plan']['kit_revision'] = 3
        self.release['downloads'] = {self.kit.name: contract(self.kit)['sha256']}
        pending = self.prepare();catalog.begin(self.root, pending)
        dest = self.root / 'docs' / pending['archive'];dest.parent.mkdir(parents=True);dest.write_bytes(self.kit.read_bytes())
        third = dict(contract(self.kit), name=self.kit.name, archive=pending['archive'].removeprefix('downloads/'))
        selected = self.publication['releases']['game']['versions']['rc1']
        selected.update(current_revision=3, files={'ips': third})
        selected['revisions']['3'] = {'files': {'ips': third}}
        self.write('data/releases.json', self.publication);self.assertTrue(catalog.recover(self.root))
        index = catalog.read_json(self.root / catalog.INDEX)
        self.assertEqual(index['projects']['game']['current'], 'game-rc1-r3')
        self.assertEqual(index['releases']['other-rc1'], self.index['releases']['other-rc1'])
        self.assertNotIn('game-rc1-r2', index['releases'])
        self.assertFalse((self.root / 'data/collection-history.json').exists())
        with self.assertRaisesRegex(ReleaseInventoryError, 'revision must increase'):
            self.prepare()
        self.assertFalse((self.root / catalog.JOURNAL).exists())

    def test_rollback_command_is_not_available(self):
        import subprocess, sys
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('import_release.py')),
            '--rollback', 'game', 'rc1', '1'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unrecognized arguments: --rollback', result.stderr)

    def test_pending_import_blocks_another_import(self):
        catalog.begin(self.root, self.prepare())
        with self.assertRaisesRegex(ReleaseInventoryError, 'unfinished'):self.prepare()


if __name__ == '__main__':unittest.main()
