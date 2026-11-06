"""Future IPS releases retain audited FBNeo names even when game bytes change."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import fbneo_names
import release_packager as packager


class FutureNamesTests(unittest.TestCase):
    def test_newly_modified_stock_member_requires_custom_name(self):
        stock={'sfx.01': b'stock', 'sfx.02': b'unchanged'}
        modified={'sfx.01': b'patched', 'sfx.02': b'unchanged'}
        with self.assertRaisesRegex(SystemExit, 'modified FBNeo member retains stock filename'):
            packager.check_fbneo_stock_names('example', {'setname':'example'}, stock, modified)
        packager.check_fbneo_stock_names('example', {'setname':'example'}, stock, stock)
        packager.check_fbneo_stock_names('example', {'setname':'example',
            'renames': {'sfx.01':'sfxuc.01'}}, stock, modified)

    def test_all_audited_ips_families_package_and_reconstruct_registered_names(self):
        site=Path(__file__).resolve().parents[1]
        pages=json.loads((site/'data/patches.json').read_text())['patches']
        registry=fbneo_names.registry()
        count=0
        for original in pages:
            if not original.get('artifact'): continue
            variants=original['artifact'].get('variants') or [original]
            if not all(v.get('hbmame',{}).get('setname') in registry for v in variants): continue
            # The existing public manifest independently declares canonical inputs.
            inventory=json.loads((site/'data/releases.json').read_text())['releases'][original['slug']]
            selected=inventory['versions'][inventory['current']]['files']['ips']
            with zipfile.ZipFile(site/'docs/downloads'/selected['archive']) as z:
                sealed=json.loads(z.read('manifest.json'))
            with self.subTest(slug=original['slug']),tempfile.TemporaryDirectory() as d:
                root=Path(d);page=copy.deepcopy(original);page.pop('mra',None);page.pop('hbmame_driver',None)
                art=page['artifact'];art.pop('auxiliary_stock_members',None)
                builds=art.get('variants') or [page]
                manifests=sealed.get('variants') or [sealed]
                stock={m['name']:('stock:'+m['name']).encode() for m in manifests[0]['members'] if m.get('role')!='device' and m['name']!='dl-1425.bin'}
                def write(path,values):
                    with zipfile.ZipFile(path,'w') as z:
                        for n,b in values.items():z.writestr(n,b)
                write(root/'stock.zip',stock);art['stock_zip']=str(root/'stock.zip')
                wanted={}
                for i,(build,manifest) in enumerate(zip(builds,manifests)):
                    build.pop('mra',None);build.pop('fbneo',None)
                    hb=build['hbmame'];declaration=registry[hb['setname']]
                    # This fixture models future content changes only under
                    # custom exported names. A separate negative control
                    # exercises newly modified members retaining stock names.
                    changed={n:target for n,target in hb['renames'].items()
                             if declaration['renames'][target] != n}
                    if not hb.get('complete'):
                        hb['renames']=changed
                    payload={n:(b'NEW'+b[3:] if n in changed else b) for n,b in stock.items()}
                    target=root/(str(i)+'.zip');write(target,payload)
                    (build if art.get('variants') else art)['patched_zip']=str(target)
                    declaration=registry[hb['setname']]
                    wanted[declaration['setname']+'.zip']={declaration['renames'][hb['renames'].get(n,n)]:b for n,b in payload.items()}
                # Synthetic ROM-name fixtures exercise the IPS producer and
                # FBNeo reconstruction; native definitions require real firmware.
                with patch.object(packager,'DOCS',root/'published'),patch('darksoft.configure_ips'), \
                     patch('kit_collection.package_archive') as native_package:
                    packager.build_rom_downloads(page)
                    native_package.assert_called_once()
                kit=root/'kit'
                with zipfile.ZipFile(root/'published/downloads'/(page['slug']+'-'+page['version']+'-ips.zip')) as z:
                    z.extractall(kit)
                    self.assertIn(b'FBNeo:',z.read('readme.txt'))
                result=subprocess.run([sys.executable,str(kit/'apply.py'),str(root/'stock.zip'),'--platform','fbneo','--out-dir',str(root/'out')],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(set(wanted),{p.name for p in (root/'out/fbneo').glob('*.zip')})
                for name,members in wanted.items():
                    with zipfile.ZipFile(root/'out/fbneo'/name) as z:
                        self.assertEqual(members,{n:z.read(n) for n in z.namelist()})
                count+=1
        self.assertGreaterEqual(count,14)
