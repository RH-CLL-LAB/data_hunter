"""Selection labels must not promote source-family evidence or software rights."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

CODE_ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(CODE_ROOT/'src'))
from datahunter.workspace import workspace_root, resolve_snapshot
ROOT=workspace_root(default=CODE_ROOT)  # the active study folder; the code root when no study exists
HAS_CATALOGUE=(ROOT/'configs/dataset_study_usage.json').is_file() and bool(json.loads((ROOT/'configs/dataset_study_usage.json').read_text()).get('datasets'))  # empty in a fresh workspace
WORKSPACE=(ROOT/'data/interim/dataset_selection/latest.json').exists()  # local acquisition workspace, not in Git
spec=importlib.util.spec_from_file_location('browser_facets',CODE_ROOT/'Scripts/dataset_browser/filter_metadata.py')
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






    def test_unresolved_package_licence_does_not_inherit_its_apache_string(self):
        self.assertEqual(facets.license_family('Apache-2.0 package; inherited component review pending'),'unknown')
        self.assertEqual(facets.file_format('hosp/labevents.csv.gz'),'csv')

    def test_all_filter_groups_and_options_have_plain_language_help(self):
        help=json.loads((CODE_ROOT/'Scripts/dataset_browser/filter_help.json').read_text())
        self.assertEqual(set(help),set(facets.FACETS))
        for key,(_,options) in facets.FACETS.items():
            self.assertGreater(len(help[key]['group']),30)
            self.assertEqual(set(help[key]['options']),set(options))
            for value in options:self.assertGreater(len(help[key]['options'][value]),30)


    def test_size_bands_have_defined_boundaries_and_unknown_is_separate(self):
        self.assertEqual(facets.size_band(None),'unknown')
        self.assertEqual(facets.size_band(0),'lt1')
        self.assertEqual(facets.size_band(2**20),'1to10')
        self.assertEqual(facets.size_band(10*2**20),'10to100')
        self.assertEqual(facets.size_band(2**30),'gte1024')


if __name__=='__main__':unittest.main()
