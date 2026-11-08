"""Current documentation must stay consolidated and separate access/readiness."""
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
HAS_CATALOGUE=bool(json.loads((ROOT/'configs/dataset_study_usage.json').read_text()).get('datasets'))  # empty in a fresh workspace
WORKSPACE=(ROOT/'data/interim/dataset_selection/latest.json').exists()  # local acquisition workspace, not in Git
spec=importlib.util.spec_from_file_location('consolidated_datasets',ROOT/'Scripts/document_dataset_selection.py')
catalogue=importlib.util.module_from_spec(spec);spec.loader.exec_module(catalogue)


@unittest.skipUnless(WORKSPACE,'needs the local acquisition workspace (data/interim receipts and licence evidence)')
@unittest.skipUnless(HAS_CATALOGUE,'needs a populated catalogue (these check the registered MedScrape entries)')
class DatasetDocumentationTests(unittest.TestCase):
    def test_public_no_registration_is_never_an_account_requirement(self):
        self.assertEqual(catalogue.access_queue({'access_route':'public_no_registration','associated_download_files':0}),'public_review')
        self.assertEqual(catalogue.access_queue({'access_route':'public_no_registration','associated_download_files':1}),'local_or_hold')

    def test_author_reference_and_full_data_registration_are_distinct(self):
        self.assertEqual(catalogue.access_queue({'access_route':'author_request_plus_source_access'}),'owner')
        self.assertEqual(catalogue.access_queue({'access_route':'registration_and_study_approval'}),'registration')

    def test_document_only_holding_does_not_become_participant_data(self):
        d={'associated_download_files':2,'category':'metadata_only','completeness':'not_obtained'}
        self.assertEqual(catalogue.availability(d),'metadata / code / reference only')
        d['associated_download_files']=0
        self.assertEqual(catalogue.availability(d),'not acquired')

    def test_active_duplicate_catalogues_and_generators_are_removed(self):
        layout=json.loads((ROOT/'configs/documentation_layout.json').read_text())
        self.assertEqual(len(layout['canonical_dataset_documents']),2)
        for path in layout['retired_documents']+layout['retired_report_generators']:
            self.assertFalse((ROOT/path).exists(),path)
        self.assertTrue((ROOT/layout['required_operational_policy']).is_file())

    def test_queue_regeneration_is_idempotent_and_does_not_submit_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'docs').mkdir()
            path=root/'docs/DATASET_ACTION_PRIORITIES.md';path.write_text('# Actions\n\nCurated priorities.\n')
            rows=[{'id':'PUBLIC','name':'Public metadata','access_route':'public_no_registration',
                   'associated_download_files':0,'action_priority':'P2','source_urls':['https://example.org'],
                   'registration_url':None,'next_action':'Review metadata.'}]
            with patch.object(catalogue,'ROOT',root):
                catalogue.update_action_queues(rows,{})
                before=path.read_text();catalogue.update_action_queues(rows,{})
            self.assertEqual(path.read_text(),before)
            registration=before.split('### Registration / institution / provider approval',1)[1].split('### Author',1)[0]
            self.assertNotIn('[PUBLIC',registration)
            self.assertIn('not submitted applications',before)
            self.assertEqual({p.name for p in (root/'docs').iterdir()},{'DATASET_ACTION_PRIORITIES.md'})

    def test_preaccess_template_screen_never_grants_ai_or_data_rights(self):
        policy=json.loads((ROOT/'configs/dataset_selection.json').read_text())
        for review in policy['preaccess_license_policy']['reviews']:
            self.assertFalse(review['rights_granted'])
        result=catalogue.preaccess_license_review({'id':'D28'},policy)
        self.assertEqual(result['status'],'public_terms_screened_conditional')
        self.assertFalse(result['rights_granted'])

    def test_preaccess_terms_are_not_inherited_by_a_different_release_or_demo(self):
        policy=json.loads((ROOT/'configs/dataset_selection.json').read_text())
        result=catalogue.preaccess_license_review({'id':'UNKNOWN_RELEASE','access_route':'registration_and_study_approval',
                                                  'optimization_status':'incomplete'},policy)
        self.assertEqual(result['status'],'not_reviewed_before_new_access')
        self.assertFalse(result['rights_granted'])

    def test_preaccess_default_accepts_raw_validation_records_before_selection(self):
        policy=json.loads((ROOT/'configs/dataset_selection.json').read_text())
        d={'id':'NEW_PUBLIC_SOURCE','access_route':'public_no_registration',
           'obtained_artifacts':[],'optimization_status':'incomplete'}
        result=catalogue.preaccess_license_review(d,policy)
        self.assertEqual(result['status'],'not_reviewed_before_new_access')

    def test_lenient_preaccess_review_does_not_require_explicit_ai_approval(self):
        policy=json.loads((ROOT/'configs/dataset_selection.json').read_text())
        self.assertIn('not newly required in lenient',policy['preaccess_license_policy']['lenient_rule'])

    def registration_records(self):
        policy=json.loads((ROOT/'configs/dataset_selection.json').read_text())
        usage=json.loads((ROOT/'configs/dataset_study_usage.json').read_text())
        priorities={i:g['priority'] for g in policy['priority_groups'] for i in g['ids']}
        return policy,[{**d,'action_priority':priorities.get(d['id'],'P3 / hold')}
                       for d in usage['datasets']]

    def test_all_current_registration_candidates_have_evidenced_outcomes(self):
        policy,rows=self.registration_records()
        coverage=catalogue.preaccess_coverage(rows,policy)
        # 2026-11-06 medscrape_devel intake added EHRCON and EHR_REASONCON (PhysioNet terms reviewed).
        self.assertEqual(coverage['current_registration_candidates'],27)
        self.assertEqual(coverage['documented_outcomes'],27)
        self.assertEqual(coverage['public_dataset_or_route_agreement_reviewed'],19)
        self.assertEqual(coverage['complete_current_package_terms_still_needed'],8)
        self.assertEqual(coverage['rights_granted'],0)
        for d in rows:
            if catalogue.access_queue(d)=='registration' and d['action_priority'] in {'P1','P2'}:
                review=catalogue.preaccess_license_review(d,policy)
                for e in review['evidence']:
                    self.assertEqual(catalogue.file_digest(ROOT/e['path']),e['sha256'])

    def test_deferred_entries_are_not_counted_as_reviewed(self):
        policy,rows=self.registration_records()
        deferred=[d for d in rows if catalogue.access_queue(d)=='registration'
                  and d['action_priority']=='P3 / hold']
        # +16 deferred credentialed/DUA candidates from the 2026-11-06 medscrape_devel from-scratch discovery.
        self.assertEqual(len(deferred),32)
        for d in deferred:
            self.assertEqual(catalogue.preaccess_license_review(d,policy)['status'],
                             'deferred_not_current_download')

    def test_new_registration_candidate_requires_coverage_review(self):
        policy,rows=self.registration_records()
        rows.append({'id':'NEW','access_route':'registration_required','action_priority':'P2'})
        with self.assertRaisesRegex(ValueError,'census_changed'):
            catalogue.preaccess_coverage(rows,policy)

    def test_missing_agreement_outcome_fails_coverage_even_if_census_matches(self):
        policy,rows=self.registration_records()
        policy['preaccess_license_policy']['reviews']=[r for r in policy['preaccess_license_policy']['reviews']
                                                       if r['id']!='ehrshot']
        with self.assertRaisesRegex(ValueError,'outcome_or_evidence_missing'):
            catalogue.preaccess_coverage(rows,policy)

    def test_stanford_and_geras_are_in_the_registration_queue(self):
        _,rows=self.registration_records()
        for d in rows:
            if d['id'] in {'EHRSHOT','GERAS_RF'}:
                self.assertEqual(catalogue.access_queue(d),'registration')
                self.assertTrue(d['registration_url'])

    def test_partial_component_review_does_not_clear_all_private_cohorts(self):
        policy,_=self.registration_records()
        r=catalogue.preaccess_license_review({'id':'ADHTEB_PRIVATE'},policy)
        self.assertEqual(len(r['component_reviews']),4)
        self.assertFalse(r['public_dataset_or_route_agreement_reviewed'])
        self.assertFalse(r['rights_granted'])


if __name__=='__main__':unittest.main()
