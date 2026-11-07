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

    def test_launcher_notice_survives_expand_and_publish(self):
        # Game imports re-publish the whole index; the newest-launcher field must ride along.
        self.model['launcher'] = {'version': '0.1.1', 'url': 'https://arcadepatches.com/arcadepatches/'}
        index, files = ci.publish(self.model, lambda url: self.kit)
        self.assertEqual(index['launcher'], self.model['launcher'])
        again, _ = ci.publish(ci.expand(index, lambda url: files[url.removeprefix(ci.ORIGIN)]), lambda url: self.kit)
        self.assertEqual(again['launcher'], self.model['launcher'])

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


class CapabilityTests(unittest.TestCase):
    def test_external_tools_requires_capability_even_when_empty(self):
        for tools in ([], [{'id': 'mame'}]):
            definition = {'api': 1, 'game_version': 'rc1', 'kit_revision': 9,
                'prerequisites': {}, 'targets': {'jp-mister': {'external_tools': tools}}}
            with self.subTest(tools=tools):
                with self.assertRaisesRegex(ValueError, 'external-tools-v1'):
                    ci.native_release(definition, 'kit')
                definition['requires'] = ['external-tools-v1']
                self.assertIn('external-tools-v1', ci.native_release(definition, 'kit')['requires'])
