import copy
import json
from unittest.mock import patch

from test_release_inventory import ReleaseInventoryTests, write_json
from release_inventory import import_candidate, load_inventory, sha256_file, ReleaseInventoryError, validate_inventory
from kit_revisions import identity, rollback, recover


class KitRevisionTests(ReleaseInventoryTests):
    def revision_candidate(self):
        ready, candidate = self.candidate()
        self.set_page_version('rc2')
        import_candidate(self.root, ready, candidate)
        old = load_inventory(self.root / 'data/releases.json')['releases']['demo']['versions']['rc2']
        # Reuse the fixture candidate as a genuinely different tooling archive.
        download = candidate / 'downloads/demo-rc2-ips.zip'
        import zipfile
        with zipfile.ZipFile(download, 'a') as z:
            z.writestr('build-info.json', '{"kit_revision":2}')
        release = json.loads((candidate / 'release.json').read_text())
        release['downloads'][download.name] = sha256_file(download)
        release['plan'].update(kit_revision=2, tooling_baseline={
            'revision': 1, 'files': {r: f['sha256'] for r, f in old['files'].items()}})
        write_json(candidate / 'release.json', release)
        record = json.loads(ready.read_text())
        record['release'] = release
        record['release_sha256'] = sha256_file(candidate / 'release.json')
        record['reproduction']['release_sha256'] = record['release_sha256']
        record['reproduction']['downloads'] = release['downloads']
        record['qa']['release_sha256'] = record['release_sha256']
        record['tooling_parity'] = {'status': 'passed', 'release_sha256': record['release_sha256'],
            'baseline': release['plan']['tooling_baseline'], 'qa_sha256': identity(record['qa']),
            'game_contract_sha256': 'b' * 64}
        write_json(ready, record)
        return ready, candidate, old

    def test_revision_and_rollback_preserve_names_and_archived_bytes(self):
        ready, candidate, old = self.revision_candidate()
        import_candidate(self.root, ready, candidate)
        inv = load_inventory(self.root / 'data/releases.json')
        self.assertEqual(2, inv['schema'])
        item = inv['releases']['demo']['versions']['rc2']
        self.assertEqual(2, item['current_revision'])
        self.assertEqual(old['files']['ips']['name'], item['files']['ips']['name'])
        validate_inventory(self.root, inv)
        rollback(self.root, 'demo', 'rc2', 1)
        restored = load_inventory(self.root / 'data/releases.json')
        self.assertEqual(old['files']['ips']['sha256'], restored['releases']['demo']['versions']['rc2']['files']['ips']['sha256'])
        rollback(self.root, 'demo', 'rc2', 2)
        validate_inventory(self.root, load_inventory(self.root / 'data/releases.json'))

    def contract_candidate(self, additive):
        ready, candidate, old = self.revision_candidate()
        inv = json.loads((self.root / 'data/releases.json').read_text())
        item = inv['releases']['demo']['versions']['rc2']
        item['game_contract_sha256'] = 'a' * 64
        for revision in item.get('revisions', {}).values():
            revision['game_contract_sha256'] = 'a' * 64
        write_json(self.root / 'data/releases.json', inv)
        if additive:
            release = json.loads((candidate / 'release.json').read_text())
            if additive == 'fbneo-names':
                release['plan']['fbneo_member_names'] = {'fbneo/demo.zip': {'game.03': 'legacy.03'}}
            elif additive == 'fbneo':
                release['plan']['add_fbneo'] = {'mame/usa/stock.zip': 'fbneo/demo.zip'}
            else:
                release['plan']['add_hbmame' if additive == 'hbmame' else 'add_darksoft'] = True
            write_json(candidate / 'release.json', release)
            record = json.loads(ready.read_text())
            digest = sha256_file(candidate / 'release.json')
            record['release'], record['release_sha256'] = release, digest
            record['reproduction']['release_sha256'] = digest
            record['qa']['release_sha256'] = digest
            record['tooling_parity'].update(release_sha256=digest, qa_sha256=identity(record['qa']))
            write_json(ready, record)
        return ready, candidate

    def test_changed_game_contract_needs_a_declared_additive_revision(self):
        ready, candidate = self.contract_candidate(False)
        with self.assertRaisesRegex(ReleaseInventoryError, 'game contract differs'):
            import_candidate(self.root, ready, candidate)

    def test_declared_additive_revision_starts_a_new_game_contract(self):
        ready, candidate = self.contract_candidate(True)
        import_candidate(self.root, ready, candidate)
        item = load_inventory(self.root / 'data/releases.json')['releases']['demo']['versions']['rc2']
        self.assertEqual('b' * 64, item['game_contract_sha256'])

    def test_declared_hbmame_revision_starts_a_new_game_contract(self):
        ready, candidate = self.contract_candidate('hbmame')
        import_candidate(self.root, ready, candidate)
        inv = load_inventory(self.root / 'data/releases.json')
        item = inv['releases']['demo']['versions']['rc2']
        self.assertEqual('hbmame', item['revisions']['2']['additive_output'])
        validate_inventory(self.root, inv)

    def test_fbneo_member_names_start_a_new_filename_contract(self):
        ready, candidate = self.contract_candidate('fbneo-names')
        import_candidate(self.root, ready, candidate)
        inv = load_inventory(self.root / 'data/releases.json')
        item = inv['releases']['demo']['versions']['rc2']
        self.assertEqual('fbneo', item['revisions']['2']['member_names'])
        self.assertNotIn('additive_output', item['revisions']['2'])
        validate_inventory(self.root, inv)

    def test_stale_baseline_or_qa_is_rejected(self):
        ready, candidate, old = self.revision_candidate()
        record = json.loads(ready.read_text())
        record['tooling_parity']['qa_sha256'] = '0' * 64
        write_json(ready, record)
        with self.assertRaisesRegex(ReleaseInventoryError, 'another QA'):
            import_candidate(self.root, ready, candidate)
        self.assertEqual(old['files']['ips']['sha256'], sha256_file(self.root / 'docs/downloads/demo-rc2-ips.zip'))

    def test_declared_fbneo_revision_starts_a_new_game_contract(self):
        ready, candidate = self.contract_candidate('fbneo')
        import_candidate(self.root, ready, candidate)
        inv = load_inventory(self.root / 'data/releases.json')
        item = inv['releases']['demo']['versions']['rc2']
        self.assertEqual('fbneo', item['revisions']['2']['additive_output'])
        validate_inventory(self.root, inv)

    def test_failure_after_alias_replacement_restores_inventory_and_download(self):
        ready, candidate, old = self.revision_candidate()
        previous = load_inventory(self.root / 'data/releases.json')
        with patch('kit_revisions.published_bundle', side_effect=ValueError('injected render failure')):
            with self.assertRaisesRegex(ValueError, 'injected'):
                import_candidate(self.root, ready, candidate)
        self.assertEqual(previous, load_inventory(self.root / 'data/releases.json'))
        self.assertEqual(old['files']['ips']['sha256'], sha256_file(self.root / 'docs/downloads/demo-rc2-ips.zip'))
        self.assertFalse((self.root / 'data/kit-revision-transaction.json').exists())

    def test_immutable_history_tampering_is_rejected(self):
        ready, candidate, old = self.revision_candidate()
        import_candidate(self.root, ready, candidate)
        inv = load_inventory(self.root / 'data/releases.json')
        file = inv['releases']['demo']['versions']['rc2']['revisions']['1']['files']['ips']
        (self.root / 'docs/downloads' / file['archive']).write_bytes(b'changed')
        with self.assertRaisesRegex(ReleaseInventoryError, 'archive missing or changed'):
            validate_inventory(self.root, inv)

    def test_recovery_from_journal_restores_previous_publication(self):
        ready, candidate, old = self.revision_candidate()
        previous = load_inventory(self.root / 'data/releases.json')
        from kit_revisions import migrate, JOURNAL
        migrate(self.root, previous)  # populate immutable originals
        write_json(self.root / JOURNAL, {'previous': previous, 'restore': list(old['files'].values())})
        (self.root / 'docs/downloads/demo-rc2-ips.zip').write_bytes(b'interrupted replacement')
        with self.assertRaisesRegex(ReleaseInventoryError, 'unfinished publication'):
            import_candidate(self.root, ready, candidate)
        recover(self.root)
        self.assertEqual(previous, load_inventory(self.root / 'data/releases.json'))
        validate_inventory(self.root, previous)
