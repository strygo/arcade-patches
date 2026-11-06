"""Inventory boundaries for the public Upper installer."""
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
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
        files={n:apply.pin(self.root/n)['sha256'] for n in (*self.record['files'],'release-kit.json')}
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

    def test_inventory_diagnostics_name_missing_and_extra_files(self):
        for native in (False, True):
            with self.subTest(native=native):
                if native:self.native()
                (self.root/'core.rbf').unlink()
                (self.root/'notes.txt').write_text('extra')
                with self.assertRaises(ValueError) as caught:apply.validate(self.root)
                text=str(caught.exception)
                self.assertIn('Missing files:\n  core.rbf',text)
                self.assertIn('Extra files:\n  notes.txt',text)
                self.assertIn('new, empty folder',text)
                (self.root/'core.rbf').write_bytes(b'fixture');(self.root/'notes.txt').unlink()

    def test_cli_validation_errors_are_clean_with_optional_debug_traceback(self):
        import shutil
        shutil.copyfile(apply.__file__,self.root/'apply.py')
        self.record['files']['apply.py']=apply.pin(self.root/'apply.py');self.save();self.native()
        (self.root/'core.rbf').unlink();(self.root/'notes.txt').write_text('extra')
        command=[sys.executable,'-I','-B',str(self.root/'apply.py'),'--rompath',str(self.root)]
        for key in ('ps2-us','ps2-jp','naomi-chd','naomi-pic','chdman','work','out-dir'):
            command += ['--'+key,str(self.root/'unused')]
        for debug in (False,True):
            with self.subTest(debug=debug):
                result=subprocess.run(command+(['--debug'] if debug else []),capture_output=True,text=True,timeout=15)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('Missing files:\n  core.rbf',result.stderr)
                self.assertIn('Extra files:\n  notes.txt',result.stderr)
                self.assertEqual('Traceback' in result.stderr,debug)
                self.assertEqual(result.stdout,'')

    def test_reconstruction_errors_keep_bounded_details_out_of_console(self):
        command=[sys.executable,'-I','-B','-c',
                 "import sys; sys.stderr.write('x'*70000+'\\n'); raise ValueError('synthetic bad media')"]
        stream=io.StringIO()
        with contextlib.redirect_stderr(stream),self.assertRaisesRegex(ValueError,'Reconstruction failed.*Details:'):
            apply.run_builder(command,self.root)
        self.assertEqual(stream.getvalue(),'')
        log=(self.root/'reconstruction-error.log').read_bytes()
        self.assertLessEqual(len(log),65536)
        self.assertIn(b'Traceback',log)
        self.assertIn(b'synthetic bad media',log)

    def test_successful_reconstruction_preserves_exact_command_and_output(self):
        command=['python','build.py','--rompath','owned-roms']
        with patch.object(apply.subprocess,'run',return_value=SimpleNamespace(returncode=0,stderr=b'')) as run:
            apply.run_builder(command,self.root)
        run.assert_called_once_with(command,stderr=subprocess.PIPE)
        self.assertFalse((self.root/'reconstruction-error.log').exists())

    def test_incomplete_native_pair_names_missing_definition(self):
        self.native();(self.root/'collection.json').unlink()
        with self.assertRaisesRegex(ValueError,'Missing files:\n  collection.json'):
            apply.validate(self.root)

    def test_inventory_diagnostic_is_sorted_and_bounded(self):
        names={f'extra-{n:02}.txt' for n in range(25)}
        with self.assertRaises(ValueError) as caught:
            apply.require_inventory(set(),names,'Release kit')
        text=str(caught.exception)
        self.assertLess(text.index('extra-00.txt'),text.index('extra-19.txt'))
        self.assertNotIn('extra-20.txt',text)
        self.assertIn('and 5 more',text)

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
