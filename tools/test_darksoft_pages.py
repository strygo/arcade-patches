"""Page claims follow selected kit declarations, with native-audio limits."""
import io
import json
import unittest
import zipfile
from release_inventory import declared_darksoft,ReleaseInventoryError
from build import render_darksoft_support


class DarksoftPages(unittest.TestCase):
    def archive(self,files):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:
            for name,value in files.items():z.writestr(name,json.dumps(value))
        return zipfile.ZipFile(io.BytesIO(data.getvalue()))

    def test_ips_declaration_is_reported_without_a_collection_definition(self):
        with self.archive({'manifest.json':{'variants':[{'key':'usa','darksoft':{'profile':'game'}},{'key':'jp'}]}}) as z:
            self.assertEqual(declared_darksoft(z),['usa'])

    def test_native_declaration_requires_actual_flash_output_contracts(self):
        with self.archive({'collection.json':{'targets':{'us-darksoft':{'platform':'darksoft','outputs':{'games/game/NAME':{},'games/game/flash.01':{}}}}}}) as z:
            self.assertEqual(declared_darksoft(z),['us-darksoft'])
        with self.archive({'collection.json':{'targets':{'us-darksoft':{'platform':'darksoft','outputs':{}}}}}) as z:
            with self.assertRaises(ReleaseInventoryError):declared_darksoft(z)

    def test_no_declaration_never_advertises_support(self):
        self.assertEqual(render_darksoft_support({'slug':'unknown'},{}),'')

    def test_native_limits_and_copy_instructions_are_visible(self):
        bundle={'darksoft_variants':['us-darksoft']}
        for slug,phrase in [('sfa2-gold','8 MB'),('sfa2-ex','8 MB'),('usf2-backport','native music'),('final-fight-ex-cps2','Phoenix-style')]:
            text=render_darksoft_support({'slug':slug},bundle)
            self.assertIn(phrase,text);self.assertIn('--platform darksoft',text)
            self.assertIn('out/darksoft/games/',text)


if __name__=='__main__':unittest.main()
