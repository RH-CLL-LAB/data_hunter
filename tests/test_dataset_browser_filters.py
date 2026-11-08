"""Selection labels must not promote source-family evidence or software rights."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
HAS_CATALOGUE=bool(json.loads((ROOT/'configs/dataset_study_usage.json').read_text()).get('datasets'))  # empty in a fresh workspace
WORKSPACE=(ROOT/'data/interim/dataset_selection/latest.json').exists()  # local acquisition workspace, not in Git
spec=importlib.util.spec_from_file_location('browser_facets',ROOT/'Scripts/dataset_browser/filter_metadata.py')
facets=importlib.util.module_from_spec(spec);spec.loader.exec_module(facets)


@unittest.skipUnless(WORKSPACE,'needs the local acquisition workspace (data/interim receipts and licence evidence)')
class BrowserFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        receipt=json.loads((ROOT/'data/interim/dataset_selection/latest.json').read_text())
        cls.datasets={d['id']:d for d in json.loads((ROOT/receipt['selection']).read_text())['datasets']}
        cls.policy=json.loads((ROOT/'configs/dataset_selection.json').read_text())
        cls.licenses=json.loads((ROOT/'configs/data_licenses.json').read_text())

    def metadata(self,id):
        return facets.metadata(self.datasets[id],self.policy,self.licenses,lambda d:'holding',
                               lambda d:'registration' if 'registration' in d['access_route'] and 'no_registration' not in d['access_route'] else 'local_or_hold')

    @unittest.skipUnless(HAS_CATALOGUE,'needs a populated catalogue (these check the registered catalogue entries)')
    def test_public_dua_does_not_become_runtime_permission(self):
        result=self.metadata('EHRSHOT')
        self.assertIn('dua',result['license'])
        self.assertIn('unknown',result['license'])
        self.assertIn('rights',result['work'])
        self.assertNotEqual(result['ready'],'ready_scoped')

    @unittest.skipUnless(HAS_CATALOGUE,'needs a populated catalogue (these check the registered catalogue entries)')
    def test_code_license_is_not_a_data_licence_filter(self):
        result=self.metadata('D41')
        self.assertNotIn('permissive',result['license'])
        self.assertIn('unknown',result['license'])

    @unittest.skipUnless(HAS_CATALOGUE,'needs a populated catalogue (these check the registered catalogue entries)')
    def test_published_evaluation_is_separate_from_current_readiness(self):
        result=self.metadata('GDC_SM')
        self.assertIn('mapping',result['benchmark'])
        self.assertNotEqual(result['ready'],'ready_scoped')
        self.assertIn('reference',result['work'])

    @unittest.skipUnless(HAS_CATALOGUE,'needs a populated catalogue (these check the registered catalogue entries)')
    def test_source_citation_alone_is_not_an_evaluation(self):
        result=self.metadata('D28')
        self.assertEqual(result['benchmark'],['context'])

    @unittest.skipUnless(HAS_CATALOGUE,'needs a populated catalogue (these check the registered catalogue entries)')
    def test_missing_size_is_not_zero_and_files_are_metadata_only(self):
        result=self.metadata('EHRSHOT')
        self.assertIsNone(result['retained_bytes'])
        self.assertEqual(result['files'],[])
        self.assertEqual(result['format'],['not_held'])

    def test_unresolved_package_licence_does_not_inherit_its_apache_string(self):
        self.assertEqual(facets.license_family('Apache-2.0 package; inherited component review pending'),'unknown')
        self.assertEqual(facets.file_format('hosp/labevents.csv.gz'),'csv')

    def test_all_filter_groups_and_options_have_plain_language_help(self):
        help=json.loads((ROOT/'Scripts/dataset_browser/filter_help.json').read_text())
        self.assertEqual(set(help),set(facets.FACETS))
        for key,(_,options) in facets.FACETS.items():
            self.assertGreater(len(help[key]['group']),30)
            self.assertEqual(set(help[key]['options']),set(options))
            for value in options:self.assertGreater(len(help[key]['options'][value]),30)

    @unittest.skipUnless(HAS_CATALOGUE,'needs a populated catalogue (these check the registered catalogue entries)')
    def test_blockers_nest_under_their_readiness_state_and_never_claim_readiness(self):
        for key,parents in facets.SUBOPTIONS.items():
            for parent,(sub_key,values) in parents.items():
                self.assertIn(parent,facets.FACETS[key][1]);self.assertIn(sub_key,facets.NESTED_FACETS)
                for value in values:self.assertIn(value,facets.FACETS[sub_key][1])
        for id,d in self.datasets.items():
            result=self.metadata(id)
            if result['ready']=='needs_preparation_or_permissions':
                self.assertTrue(result['blocker']);self.assertTrue(all(b.startswith('prep_') for b in result['blocker']),id)
            elif result['ready']=='incomplete':
                self.assertTrue(result['blocker']);self.assertTrue(all(b.startswith('inc_') for b in result['blocker']),id)
            else:self.assertEqual(result['blocker'],[],id)
        self.assertIn('prep_rights',self.metadata('GDC_SM')['blocker'])
        self.assertIn('inc_access',self.metadata('EHRSHOT')['blocker'])

    def test_size_bands_have_defined_boundaries_and_unknown_is_separate(self):
        self.assertEqual(facets.size_band(None),'unknown')
        self.assertEqual(facets.size_band(0),'lt1')
        self.assertEqual(facets.size_band(2**20),'1to10')
        self.assertEqual(facets.size_band(10*2**20),'10to100')
        self.assertEqual(facets.size_band(2**30),'gte1024')


if __name__=='__main__':unittest.main()
