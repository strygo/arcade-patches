"""Native sidecars are exact kit members, with no second authored recipe."""
import copy
import io
import json
import unittest
import zipfile
import catalog_index as ci


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.definition = {'api': 1, 'project': 'game', 'game_version': 'rc1', 'kit_revision': 1,
            'prerequisites': {'rom': {}}, 'targets': {'mame': {'outputs': {'game.zip': {}}}}}
        self.raw = json.dumps(self.definition, indent=4).encode()  # Deliberately noncanonical.
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:archive.writestr('collection.json', self.raw)
        self.kit = out.getvalue()
        self.model = {'schema': 1, 'generation': 1, 'projects': {'game': {'title': 'Game', 'current': 'game-r1'}},
            'artifacts': {'game-kit': {'url': 'kit'}},
            'releases': {'game-r1': ci.native_release(self.definition, 'game-kit')}}

    def test_exact_bytes_and_lossless_roundtrip(self):
        index, files = ci.publish(self.model, lambda url: self.kit)
        self.assertEqual(list(files.values()), [self.raw])
        pointer = index['releases']['game-r1']
        self.assertNotIn('targets', pointer);self.assertNotIn('prerequisites', pointer)
        self.assertEqual(ci.expand(index, lambda url: files[url.removeprefix(ci.ORIGIN)]), self.model)

    def test_native_contract_drift_rejected(self):
        self.model['releases']['game-r1']['targets']['mame']['outputs'] = {}
        with self.assertRaisesRegex(ValueError, 'qualified release'):ci.publish(self.model, lambda url: self.kit)

    def test_sidecar_hash_and_revision_binding(self):
        index, files = ci.publish(self.model, lambda url: self.kit)
        with self.assertRaisesRegex(ValueError, 'Definition differs'):ci.expand(index, lambda url: b'{}')
        index['releases']['game-r1']['kit_revision'] = 2
        with self.assertRaisesRegex(ValueError, 'revision differs'):
            ci.expand(index, lambda url: files[url.removeprefix(ci.ORIGIN)])

    def test_adapter_release_is_rejected(self):
        release = self.model['releases']['game-r1'];release['adapter'] = release.pop('runner')
        with self.assertRaisesRegex(ValueError, 'Only native'):
            ci.publish(self.model, lambda url: self.fail('Invalid model must not read a kit'))
