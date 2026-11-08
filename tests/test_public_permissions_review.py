"""Scoped public-source review must not clear a mixed parent or restricted instrument."""
import json
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
HAS_CATALOGUE=bool(json.loads((ROOT/'configs/dataset_study_usage.json').read_text()).get('datasets'))  # empty in a fresh workspace

@unittest.skipUnless(HAS_CATALOGUE,'needs a populated catalogue (these check the registered MedScrape entries)')
class PublicPermissionsReviewTests(unittest.TestCase):
    def setUp(self):
        self.resources={r['id']:r for r in json.loads((ROOT/'configs/data_licenses.json').read_text())['resources']}
        self.assets=json.loads((ROOT/'configs/data_license_assets.json').read_text())['files']
    def test_local_review_does_not_invent_explicit_ai_or_remote_approval(self):
        for name in ['synthea','heal_source_terms','pharmaverse_generated_response_examples']:
            r=self.resources[name]
            self.assertEqual(r['operations']['local_inference'],'allowed')
            self.assertEqual(r['ai_permissions']['local_inference'],'unreviewed')
            for op in ['remote_inference','training','redistribution']:self.assertEqual(r['operations'][op],'unreviewed')
    def test_mixed_parent_and_instrument_keep_separate_decisions(self):
        for name in ['pharmaverse_raw','pharmaverse_sdtm','cdisc_pilot','heal']:
            self.assertEqual(self.resources[name]['operations']['local_inference'],'unreviewed')
        self.assertEqual(self.resources['pharmaverse_coeq']['operations']['local_inference'],'prohibited')
        self.assertEqual(self.resources['pharmaverse_coeq']['ai_permissions']['local_inference'],'unreviewed')
    def test_only_three_response_csv_hashes_are_in_generated_subset(self):
        selected={Path(a['path']).name for a in self.assets if a['resource']=='pharmaverse_generated_response_examples' and '/prepared/' in a['path']}
        self.assertEqual(selected,{'rs_onco_irecist.csv','rs_onco_ca125.csv','supprs_onco_ca125.csv'})
        source=next(a for a in self.assets if a['path'].endswith('/schema_inputs/heal/cde_terms.csv'))
        self.assertEqual(source['resource'],'heal_source_terms')

if __name__=='__main__':unittest.main()
