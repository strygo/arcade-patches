"""Gold/EX naming exports exercise real publication paths with synthetic recipes."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import fbneo_names
spec=importlib.util.spec_from_file_location('gold_apply_under_test',HERE/'apply.py')
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)


class NativeNamingTests(unittest.TestCase):
    def test_all_six_gold_and_ex_regions_keep_native_outputs_and_export_fbneo(self):
        configurations=[('Gold',app.SETS),('EX',json.loads((HERE.parent/'sfa2ex_builder/kit.json').read_text())['sets'])]
        for title,sets in configurations:
            for region,names in sets.items():
                for platform in ('all','fbneo'):
                    with self.subTest(title=title,region=region,platform=platform),tempfile.TemporaryDirectory() as d:
                        root=Path(d);(root/'recipes').mkdir();disc=root/'disc.iso';disc.write_bytes(b'disc')
                        if title=='EX':(root/'kit.json').write_text('{}')
                        mapping=fbneo_names.registry()[names['8mb']]['renames']
                        members={n:('fixture:'+n).encode() for n in mapping}
                        md5={n:hashlib.md5(b).hexdigest() for n,b in members.items()}
                        (root/'recipes'/(region+'.json')).write_text(json.dumps({'sizes':{s:{'recipes':{},'md5':md5} for s in ('4mb','8mb')}}))
                        (root/'rom_inputs.json').write_text(json.dumps({'sets':{'sfz2al':{},'sfz2alj':{}}}))
                        argv=['apply.py','--region',region,'--iso',str(disc),'--rompath',str(root),'--platform',platform,'--out-dir',str(root/'out')]
                        with patch.object(app,'HERE',root),patch.object(app,'SETS',sets),patch.object(app,'TITLE',title),patch('sys.argv',argv), \
                             patch.object(app,'Resolver') as resolver,patch.object(app.assemble,'missing_inputs',return_value=[]), \
                             patch.object(app.ex,'extract_zero6',return_value={'entry531':b'','comp2':b''}),patch.object(app.ex,'extract_audio',return_value=b''), \
                             patch.object(app.assemble,'prepare_inputs',return_value={}),patch.object(app.assemble,'reconstruct',return_value=members), \
                             patch('darksoft.tree',return_value={}),patch('darksoft.native_menu',return_value='Fixture'):
                            resolver.return_value.resolve.return_value={};resolver.return_value.devices.return_value={}
                            app.main()
                        alias=fbneo_names.registry()[names['8mb']]['setname']
                        with zipfile.ZipFile(root/'out/fbneo'/(alias+'.zip')) as z:
                            self.assertEqual({mapping[n]:b for n,b in members.items()},{n:z.read(n) for n in z.namelist()})
                        if platform=='fbneo':self.assertEqual([p.name for p in (root/'out').iterdir()],['fbneo'])
                        else:
                            with zipfile.ZipFile(root/'out/hbmame'/(names['8mb']+'.zip')) as z:
                                self.assertEqual(members,{n:z.read(n) for n in z.namelist()})


if __name__=='__main__':unittest.main()
