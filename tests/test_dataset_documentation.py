"""Current documentation must stay consolidated and separate access/readiness."""
import importlib.util
import json
from pathlib import Path
import sys
import re
import tempfile
import unittest
from unittest.mock import patch

CODE_ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(CODE_ROOT/'src'))
from datahunter.workspace import workspace_root, resolve_snapshot
ROOT=workspace_root(default=CODE_ROOT)  # the active study folder; the code root when no study exists
HAS_CATALOGUE=(ROOT/'configs/dataset_study_usage.json').is_file() and bool(json.loads((ROOT/'configs/dataset_study_usage.json').read_text()).get('datasets'))  # empty in a fresh workspace
WORKSPACE=(ROOT/'data/interim/dataset_selection/latest.json').exists()  # local acquisition workspace, not in Git
spec=importlib.util.spec_from_file_location('consolidated_datasets',CODE_ROOT/'Scripts/document_dataset_selection.py')
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



    def test_new_registration_candidate_requires_coverage_review(self):
        policy,rows=self.registration_records()
        rows.append({'id':'NEW','access_route':'registration_required','action_priority':'P2'})
        with self.assertRaisesRegex(ValueError,'census_changed'):
            catalogue.preaccess_coverage(rows,policy)


    def test_stanford_and_geras_are_in_the_registration_queue(self):
        _,rows=self.registration_records()
        for d in rows:
            if d['id'] in {'EHRSHOT','GERAS_RF'}:
                self.assertEqual(catalogue.access_queue(d),'registration')
                self.assertTrue(d['registration_url'])



if __name__=='__main__':unittest.main()
