"""Inventory boundaries for the public Upper installer."""
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import subprocess
import shutil
import sys
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import apply


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='upper-installer-test-')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        shutil.copyfile(Path(apply.__file__).resolve().parents[1]/'tool_controls.py',self.root/'tool_controls.py')
        for name in ('names.json','core.rbf','final.py','final.json'):(self.root/name).write_bytes(b'fixture')
        self.record={'schema':1,'files':{n:apply.pin(self.root/n) for n in ('names.json','core.rbf','final.py','final.json')},'names':'names.json','core':'core.rbf','release_final':{'entrypoint':'final.py','recipe':'final.json'}}
        self.save()

    def save(self):
        (self.root/'release-kit.json').write_text(json.dumps(self.record))

    def test_complete_inventory(self):
        self.assertEqual(apply.validate(self.root),self.record)

    def test_changed_and_missing_files_fail_but_extras_are_allowed(self):
        (self.root/'core.rbf').write_bytes(b'changed')
        with self.assertRaises(ValueError):apply.validate(self.root)
        (self.root/'core.rbf').write_bytes(b'fixture');(self.root/'extra').write_bytes(b'x')
        self.assertEqual(apply.validate(self.root),self.record)
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
        self.assertEqual(apply.validate(self.root),self.record)

    def test_inventory_diagnostics_name_missing_files_and_allow_notes(self):
        for native in (False, True):
            with self.subTest(native=native):
                if native:self.native()
                (self.root/'core.rbf').unlink()
                (self.root/'notes.txt').write_text('extra')
                with self.assertRaises(ValueError) as caught:apply.validate(self.root)
                text=str(caught.exception)
                self.assertIn('Missing files:\n  core.rbf',text)
                self.assertNotIn('notes.txt',text)
                self.assertIn('original ZIP',text)
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
                self.assertNotIn('notes.txt',result.stderr)
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
        self.native()
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
        out=self.root/'out'
        import shutil
        self.addCleanup(lambda:shutil.rmtree(out,ignore_errors=True))
        with patch.object(apply,'module',module):apply.postprocess(self.root,archives,out,('hbmame',))
        self.assertTrue((out/'verification.json').is_file())
        self.assertEqual(apply.validate(self.root),self.record)

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
        self.assertEqual(apply.validate(self.root),self.record)
        (self.root/'core.rbf').unlink()
        (self.root/'core.rbf').symlink_to(self.root/'names.json')
        with self.assertRaises(ValueError):apply.validate(self.root)
        for name in ('../escape','/absolute','folder/../escape','folder\\escape'):
            with self.assertRaises(ValueError):apply.local(self.root,name)

    def paths(self):
        inputs=self.root/'input';inputs.mkdir()
        for name in ('us.iso','jp.iso','naomi.chd','pic.zip'):(inputs/name).write_bytes(b'source')
        for name in ('us.iso','jp.iso'):(inputs/name).write_bytes(b'\0'*0x8000+b'\x01CD001\x01')
        roms=self.root/'roms';roms.mkdir();(roms/'sfa3.zip').write_bytes(b'roms')
        tools=self.root/'chdman';tools.mkdir()
        executable=tools/'chdman.exe';executable.write_bytes(b'tool');executable.chmod(0o755)
        return SimpleNamespace(ps2_us=inputs/'us.iso',ps2_jp=inputs/'jp.iso',
            naomi_chd=inputs/'naomi.chd',naomi_pic=inputs/'pic.zip',rompath=[roms],
            chdman=tools,work=self.root/'work',out_dir=self.root/'out')

    def test_reported_layout_allows_inputs_tools_and_generated_files(self):
        self.native();args=self.paths()
        with patch.object(Path,'rglob',side_effect=AssertionError('Do not scan user media')):
            self.assertEqual(apply.validate(self.root),self.record)
            apply.prepare_paths(args,self.root,self.record)
        self.assertEqual(args.chdman,self.root/'chdman/chdman.exe')
        self.assertFalse(args.work.exists());self.assertFalse(args.out_dir.exists())
        args.work.mkdir();(args.work/'log.txt').write_text('generated')
        args.out_dir.mkdir();(args.out_dir/'game.zip').write_bytes(b'generated')
        self.assertEqual(apply.validate(self.root),self.record)

    def test_main_uses_resolved_tool_and_in_kit_destinations(self):
        args=self.paths();calls=[]
        def rebuild(command,work,debug):
            calls.append(command)
            (work/'rebuilt').mkdir()
        argv=['apply.py']
        for key,value in vars(args).items():
            for path in value if isinstance(value,list) else [value]:
                argv += ['--'+key.replace('_','-'),str(path)]
        with patch.object(apply,'__file__',str(self.root/'apply.py')),patch.object(sys,'argv',argv),\
             patch.object(apply,'check_python') as runtime,\
             patch.object(apply,'run_builder',side_effect=rebuild),\
             patch.object(apply,'postprocess',return_value={}) as post:
            apply.main()
        runtime.assert_called_once_with()
        self.assertEqual(calls[0][calls[0].index('--chdman')+1],str(self.root/'chdman/chdman.exe'))
        self.assertEqual(post.call_args.args[2],self.root/'out')
        self.assertEqual(apply.validate(self.root),self.record)

    def test_destinations_cannot_reuse_or_alias_existing_paths(self):
        args=self.paths();original=args.work
        for target in (args.ps2_us,self.root/'input',self.root/'core.rbf',self.root):
            args.work=target
            with self.subTest(target=target),self.assertRaises(ValueError):
                apply.prepare_paths(args,self.root,self.record)
        args.work=original;args.out_dir=args.work
        with self.assertRaisesRegex(ValueError,'must be separate'):
            apply.prepare_paths(args,self.root,self.record)
        self.assertFalse(args.work.exists())

    def test_missing_inputs_or_tools_fail_before_workspace_creation(self):
        args=self.paths();args.ps2_us.unlink()
        with self.assertRaisesRegex(ValueError,'Missing input --ps2-us'):
            apply.prepare_paths(args,self.root,self.record)
        self.assertFalse(args.work.exists())
        with self.assertRaisesRegex(ValueError,'folder contains none'):
            apply.resolve_chdman(self.root/'roms')

    def test_chdman_accepts_executable_and_path_lookup(self):
        args=self.paths();executable=args.chdman/'chdman.exe'
        self.assertEqual(apply.resolve_chdman(executable),executable)
        with patch.object(apply.shutil,'which',return_value=str(executable)):
            self.assertEqual(apply.resolve_chdman('chdman'),executable)

    def test_nested_required_file_symlinks_are_rejected(self):
        (self.root/'linked').symlink_to(self.root,target_is_directory=True)
        self.record['files']['linked/core.rbf']=self.record['files']['core.rbf'];self.save()
        with self.assertRaisesRegex(ValueError,'symlink'):
            apply.validate(self.root)

    def test_output_aliases_and_missing_parents_fail_without_writes(self):
        args=self.paths();alias=self.root/'alias'
        alias.symlink_to(args.ps2_us)
        args.out_dir=alias
        with self.assertRaisesRegex(ValueError,'already exists'):
            apply.prepare_paths(args,self.root,self.record)
        alias.unlink();alias.symlink_to(self.root/'new-output')
        with self.assertRaisesRegex(ValueError,'already exists'):
            apply.prepare_paths(args,self.root,self.record)
        args.out_dir=self.root/'missing-parent/out'
        with self.assertRaisesRegex(ValueError,'Parent directory'):
            apply.prepare_paths(args,self.root,self.record)
        self.assertFalse(args.work.exists())

    def test_newer_python_gets_side_by_side_install_guidance(self):
        with patch.object(apply.sys,'version_info',(3,14)),\
             patch.object(apply.subprocess,'run') as run,\
             self.assertRaisesRegex(ValueError,'alongside your current Python'):
            apply.check_python()
        run.assert_not_called()

    def test_dependency_check_uses_running_interpreter_and_exact_versions(self):
        with patch.object(apply.sys,'version_info',(3,13)),\
             patch.object(apply.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            apply.check_python()
        self.assertEqual(run.call_args.args[0][:4],[sys.executable,'-I','-B','-c'])
        self.assertIn("('2.2.6', '1.15.3')",run.call_args.args[0][-1])
        with patch.object(apply.sys,'version_info',(3,13)),\
             patch.object(apply.subprocess,'run',return_value=SimpleNamespace(returncode=1)),\
             self.assertRaisesRegex(ValueError,'py -3.13 -m pip install'):
            apply.check_python()

if __name__=='__main__':unittest.main()
