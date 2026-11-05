"""Inventory boundaries for the public Upper installer."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch
import apply


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='upper-installer-test-')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for name in ('names.json','core.rbf','final.py','final.json'):(self.root/name).write_bytes(b'fixture')
        self.record={'schema':1,'files':{n:apply.pin(self.root/n) for n in ('names.json','core.rbf','final.py','final.json')},'names':'names.json','core':'core.rbf','release_final':{'entrypoint':'final.py','recipe':'final.json'}}
        self.save()

    def save(self):
        (self.root/'release-kit.json').write_text(json.dumps(self.record))

    def test_complete_inventory(self):
        self.assertEqual(apply.validate(self.root),self.record)

    def test_changed_extra_and_missing(self):
        (self.root/'core.rbf').write_bytes(b'changed')
        with self.assertRaises(ValueError):apply.validate(self.root)
        (self.root/'core.rbf').write_bytes(b'fixture');(self.root/'extra').write_bytes(b'x')
        with self.assertRaises(ValueError):apply.validate(self.root)
        (self.root/'extra').unlink();(self.root/'core.rbf').unlink()
        with self.assertRaises(ValueError):apply.validate(self.root)

    def native(self, **overrides):
        files={n:apply.pin(self.root/n)['sha256'] for n in ('names.json','core.rbf','final.py','final.json','release-kit.json')}
        files.update(overrides)
        (self.root/'manifest.json').write_text(json.dumps({'api':1,'files':files}))
        (self.root/'collection.json').write_text('{}')

    def test_builder_app_pair(self):
        self.native()
        self.assertEqual(apply.validate(self.root),self.record)

    def test_builder_app_pair_is_complete_and_pinned(self):
        (self.root/'collection.json').write_text('{}')
        with self.assertRaises(ValueError):apply.validate(self.root)
        self.native(**{'core.rbf':'0'*64})
        with self.assertRaises(ValueError):apply.validate(self.root)
        self.native();(self.root/'extra').write_bytes(b'x')
        with self.assertRaises(ValueError):apply.validate(self.root)

    def test_final_leaf_precedes_platform_naming(self):
        for name in ('names.json','final.json'):(self.root/name).write_text('{}')
        self.record['files']={n:apply.pin(self.root/n) for n in self.record['files']};self.save()
        archives={}
        for region in ('us','jp'):
            path=self.root.parent/(self.root.name+'-'+region+'.zip')
            self.addCleanup(path.unlink)
            with zipfile.ZipFile(path,'w') as archive:
                for i in range(26):archive.writestr(str(i),b'original')
            archives[region]=path
        calls=[]
        def transform(members,region,recipe):
            self.assertEqual(set(members.values()),{b'original'})
            calls.append(region);return {name:b'final' for name in members}
        def naming(contract,inputs,out):
            self.assertEqual(set(calls),{'us','jp'})
            for path in inputs.values():
                with zipfile.ZipFile(path) as archive:
                    self.assertEqual({archive.read(n) for n in archive.namelist()},{b'final'})
            (out/'hbmame').mkdir(parents=True)
        def module(path,name):
            return SimpleNamespace(apply_release=transform) if name=='upper_release_final' else SimpleNamespace(export_archives=naming)
        out=self.root.parent/(self.root.name+'-out')
        import shutil
        self.addCleanup(lambda:shutil.rmtree(out,ignore_errors=True))
        with patch.object(apply,'module',module):apply.postprocess(self.root,archives,out,('hbmame',))
        self.assertTrue((out/'verification.json').is_file())

    def test_published_text_uses_lf(self):
        # Without newline='\n', Windows writes verification.json with CRLF and its pinned contract differs.
        import ast
        tree=ast.parse(Path(apply.__file__).read_text())
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and getattr(n.func,'attr','')=='write_text']
        self.assertTrue(calls)
        for call in calls:
            newline=[k.value for k in call.keywords if k.arg=='newline']
            self.assertTrue(newline and isinstance(newline[0],ast.Constant) and newline[0].value=='\n',call.lineno)

    def test_alias_and_escape(self):
        (self.root/'alias').symlink_to(self.root/'core.rbf')
        with self.assertRaises(ValueError):apply.validate(self.root)
        for name in ('../escape','/absolute','folder/../escape','folder\\escape'):
            with self.assertRaises(ValueError):apply.local(self.root,name)

if __name__=='__main__':unittest.main()
