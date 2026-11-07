"""CPS+ static assets and per-soundtrack generated outputs bind complete QA."""
import copy
import unittest

from collection_catalog import verify_native_outputs
from release_inventory import ReleaseInventoryError


class CPSOutputTests(unittest.TestCase):
    def setUp(self):
        self.core = {'sha256': 'a'*64}
        self.pack = {'members': {'fixture.cpk': 'b'*64}}
        self.alternate = {'members': {'fixture.cpk': 'c'*64}}
        self.generated = {'games/CPS+/fixture.zip': {'any_of': [self.pack, self.alternate]},
                          'games/CPS+/README.txt': {'sha256': 'd'*64}}
        self.definition = {'native_outputs': {'_Arcade/cores/core.rbf': self.core},
            'targets': {'us': {'outputs': self.generated}, 'jp': {'outputs': copy.deepcopy(self.generated)}}}
        self.qualified = {'_Arcade/cores/core.rbf': self.core, **self.generated, 'games/CPS+/fixture.zip': self.pack}

    def test_complete_static_and_generated_union_and_accepted_alternate(self):
        verify_native_outputs(self.definition, self.qualified, 'cps-plus')
        self.qualified['games/CPS+/fixture.zip'] = self.alternate
        verify_native_outputs(self.definition, self.qualified, 'cps-plus')

    def test_changed_core_or_pack_or_readme_is_rejected(self):
        for path in self.qualified:
            changed = copy.deepcopy(self.qualified)
            changed[path] = {'sha256': 'e'*64}
            with self.assertRaisesRegex(ReleaseInventoryError, 'independent release QA'):
                verify_native_outputs(self.definition, changed, 'cps-plus')

    def test_unknown_or_missing_qualified_output_is_rejected(self):
        changed = dict(self.qualified);changed['games/CPS+/unknown.zip'] = self.pack
        with self.assertRaises(ReleaseInventoryError):verify_native_outputs(self.definition, changed, 'cps-plus')
        del changed['games/CPS+/unknown.zip'];del changed['games/CPS+/fixture.zip']
        with self.assertRaises(ReleaseInventoryError):verify_native_outputs(self.definition, changed, 'cps-plus')

    def test_regional_contract_conflict_is_rejected(self):
        self.definition['targets']['jp']['outputs']['games/CPS+/fixture.zip'] = self.alternate
        with self.assertRaisesRegex(ReleaseInventoryError, 'conflicting CPS'):
            verify_native_outputs(self.definition, self.qualified, 'cps-plus')

    def test_unexpected_archive_member_is_rejected(self):
        self.definition['targets']['us']['outputs']['games/CPS+/fixture.zip'] = {'members': {'other.cpk': 'b'*64}}
        with self.assertRaisesRegex(ReleaseInventoryError, 'pack contract'):
            verify_native_outputs(self.definition, self.qualified, 'cps-plus')

    def test_other_projects_retain_exact_contract_check(self):
        with self.assertRaisesRegex(ReleaseInventoryError, 'independent release QA'):
            verify_native_outputs(self.definition, self.qualified, 'other')
        verify_native_outputs(self.definition, self.definition['native_outputs'], 'other')


if __name__ == '__main__':unittest.main()
