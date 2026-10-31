"""Native FBNeo names appear only when the selected kit declares them."""
import copy
import hashlib
import io
import json
from pathlib import Path
import unittest
import zipfile
import build
import fbneo_names
from release_inventory import native_fbneo_sets, ReleaseInventoryError


class NativeFbneoTests(unittest.TestCase):
    def test_complete_audited_native_names_and_rejecting_controls(self):
        definitions={n:r for n,r in fbneo_names.registry().items() if r['slug']=='sfa2-gold'}
        patch={'slug':'sfa2-gold','builds':[{'hbmame_set':n} for n in definitions]}
        for mutation in ('none','legacy','hash','missing-region','changed-alias'):
            with self.subTest(mutation=mutation):
                stored=copy.deepcopy(definitions)
                if mutation=='missing-region': stored.pop(next(iter(stored)))
                if mutation=='changed-alias': next(iter(stored.values()))['setname']='wrong'
                data=json.dumps({'schema':1,'sets':stored}).encode()
                marker={'schema':1,'registry_sha256':hashlib.sha256(data).hexdigest()}
                if mutation=='hash': marker['registry_sha256']='0'*64
                stream=io.BytesIO()
                with zipfile.ZipFile(stream,'w') as z:
                    z.writestr('fbneo_names.json',data)
                    if mutation!='legacy':z.writestr('fbneo-exports.json',json.dumps(marker))
                with zipfile.ZipFile(stream) as z:
                    if mutation=='none':self.assertEqual(native_fbneo_sets(z,patch),{n:r['setname'] for n,r in definitions.items()})
                    elif mutation=='legacy':self.assertEqual(native_fbneo_sets(z,patch),{})
                    else:
                        with self.assertRaises(ReleaseInventoryError):native_fbneo_sets(z,patch)

    def test_native_page_table_and_instructions_preserve_both_platform_names(self):
        root=Path(__file__).resolve().parents[1]
        config=json.loads((root/'data/patches.json').read_text())
        inventory=build.load_inventory(root/'data/releases.json')
        for slug in ('sfa2-gold','sfa2-ex','final-fight-ex-cps2'):
            patch=copy.deepcopy(next(p for p in config['patches'] if p['slug']==slug))
            patch['version']=inventory['releases'][slug]['current']
            kit=build.published_bundle(root,patch,inventory)
            legacy=copy.deepcopy(kit);legacy.pop('fbneo_sets',None)
            self.assertNotIn('FBNeo',build.render_reconstruction(patch,patch['builds'],legacy))
            names={n:r['setname'] for n,r in fbneo_names.registry().items() if r['slug']==slug}
            kit['fbneo_sets']=names
            html=build.render_reconstruction(patch,patch['builds'],kit)
            self.assertIn('<th>FBNeo</th>',html)
            self.assertIn('out/fbneo/',html)
            self.assertIn('--platform fbneo',html)
            for old,new in names.items():
                self.assertIn(f'<td class="mono">{old}</td>',html)
                self.assertIn(f'<td class="mono">{new}</td>',html)


if __name__=='__main__':unittest.main()
