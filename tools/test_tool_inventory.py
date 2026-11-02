"""Focused checks for the new tool publication boundary."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from tool_inventory import contract, import_tool, local_path, validate
from catalog_index import native_release


class ToolImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        self.docs = self.root / 'docs'
        self.docs.mkdir()
        self.catalog = self.root / 'prepared'
        (self.catalog / 'artifacts').mkdir(parents=True)
        (self.root / 'data/shot.png').write_bytes(b'capture')
        self.entry = {'slug': 'arcadepatches', 'version': '0.1', 'title': 'ArcadePatches',
            'summary': 'Builder', 'date': '2026-11-01', 'features': ['Build'], 'requirements': ['Python'], 'steps': ['Launch'],
            'screenshots': [{'single': 'data/shot.png', 'caption': 'Menu'}], 'hidden': True}
        self.write('data/tools.json', self.entry)
        self.kit = self.docs / 'downloads/archive/kit.zip'
        self.kit.parent.mkdir(parents=True)
        definition = {'api': 1, 'project': 'game', 'game_version': '1', 'kit_revision': 1, 'prerequisites': {'rom': {}}, 'targets': {'default': {'outputs': {'file': {}}}}}
        with zipfile.ZipFile(self.kit, 'w') as z:z.writestr('collection.json', json.dumps(definition))
        self.write('data/releases.json', {'releases': {'game': {'versions': {'1': {'files': {
            'kit': dict(contract(self.kit), archive='archive/kit.zip')}}}}}})
        self.archive = self.root / 'ArcadePatches-0.1.zip'
        with zipfile.ZipFile(self.archive, 'w') as out:
            out.writestr('arcadepatches.py', 'print("launcher")\n')
            out.writestr('QUICKSTART.txt', 'Launch instructions\n')
        with zipfile.ZipFile(self.archive) as source:
            files = {n: {'size': len(source.read(n)), 'sha256': hashlib.sha256(source.read(n)).hexdigest()} for n in source.namelist()}
        self.write('receipt.json', dict(contract(self.archive), status='passed', version='0.1.0',
            standalone_doctor='passed', archive=self.archive.name, files=files))
        self.helper = self.catalog / ('artifacts/' + hashlib.sha256(b'helper').hexdigest() + '-helper.zip')
        self.helper.write_bytes(b'helper')
        self.index = {'schema': 1, 'generation': 1, 'projects': {'game': {'title': 'Game', 'current': 'game-1'}},
            'releases': {'game-1': native_release(definition, 'kit')},
            'provenance': {'site_commit': 'published'}, 'artifacts': {
                'kit': dict(contract(self.kit), url='https://arcadepatches.com/catalog/v1/artifacts/unused-kit.zip'),
                'helper': dict(contract(self.helper), url='https://arcadepatches.com/catalog/v1/' + str(self.helper.relative_to(self.catalog)))}}
        self.seal()

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def seal(self):
        self.write('prepared/index.json', self.index)
        self.write('qualification.json', {'status': 'passed', 'catalog_sha256': hashlib.sha256(
            json.dumps(self.index, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'site_commit': 'published', 'sources': {'arcadepatches.py': json.loads((self.root / 'receipt.json').read_text())['files']['arcadepatches.py']['sha256']}})

    def run_import(self):
        return import_tool(self.root, self.archive, self.root / 'receipt.json', self.catalog, self.root / 'qualification.json')

    def test_import_reuses_game_kit_and_preserves_game_inventory(self):
        original = (self.root / 'data/releases.json').read_bytes()
        record = self.run_import()
        entry = json.loads((self.root / 'data/tools.json').read_text())
        self.assertEqual(validate(self.root, entry), record)
        self.assertEqual(original, (self.root / 'data/releases.json').read_bytes())
        index = json.loads((self.docs / 'catalog/v1/index.json').read_text())
        self.assertEqual(index['artifacts']['kit']['url'], 'https://arcadepatches.com/downloads/archive/kit.zip')
        self.assertEqual(len(list((self.docs / 'catalog/v1/artifacts').iterdir())), 1)
        with self.assertRaisesRegex(ValueError, 'generation must increase'):
            self.run_import()

    def test_missing_dependency_fails_before_publication(self):
        self.helper.unlink()
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            self.run_import()
        self.assertFalse((self.docs / 'tools').exists())
        self.assertFalse((self.root / 'data/tool-releases.json').exists())

    def test_qualification_mismatch_rejected(self):
        self.index['generation'] = 2
        self.write('prepared/index.json', self.index)
        with self.assertRaisesRegex(ValueError, 'qualification does not match'):
            self.run_import()
        self.assertFalse((self.docs / 'tools').exists())

    def test_tampered_hosted_dependency_rejected_at_build(self):
        self.run_import()
        self.kit.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            validate(self.root, json.loads((self.root / 'data/tools.json').read_text()))

    def test_new_launcher_content_keeps_the_previous_immutable_download(self):
        first = self.run_import()
        old_path = self.docs / first['launcher']
        old_bytes = old_path.read_bytes()
        with zipfile.ZipFile(self.archive, 'w') as out:
            out.writestr('arcadepatches.py', 'print("new launcher")')
            out.writestr('QUICKSTART.txt', 'Instructions')
        receipt = json.loads((self.root / 'receipt.json').read_text())
        with zipfile.ZipFile(self.archive) as source:
            receipt['files'] = {n: {'size': len(source.read(n)), 'sha256': hashlib.sha256(source.read(n)).hexdigest()}
                                for n in source.namelist()}
        receipt.update(contract(self.archive));self.write('receipt.json', receipt)
        self.index['generation'] += 1;self.seal()
        second = self.run_import()
        self.assertNotEqual(first['launcher'], second['launcher'])
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertEqual((self.docs / second['launcher']).read_bytes(), self.archive.read_bytes())

    def test_unsafe_paths_rejected(self):
        for name in ('../outside', '/absolute', 'a/../../escape', 'a\\b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                local_path(self.docs, name)


if __name__ == '__main__':
    unittest.main()
