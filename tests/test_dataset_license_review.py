"""Manual decisions are scoped to exact files and cannot waive source restrictions."""
from copy import deepcopy
from datetime import date, timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('manual_review', ROOT / 'Scripts/review_dataset_license.py')
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)

class LicenseReviewTests(unittest.TestCase):
    def setUp(self):
        quiet = patch('builtins.print'); quiet.start(); self.addCleanup(quiet.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for directory in ['configs', 'data/interim/dataset_selection', 'data/raw/fixture']:
            (self.root / directory).mkdir(parents=True)
        (self.root / 'data/raw/fixture/source.csv').write_text('fixture only\n')
        (self.root / 'data/raw/fixture/LICENSE.txt').write_text('Fixture permission evidence, not an actual source grant.\n')
        self.asset = {'path': 'data/raw/fixture/source.csv', 'sha256': review.digest(self.root / 'data/raw/fixture/source.csv')}
        self.catalogue = self.root / 'data/interim/dataset_selection/selection.json'
        self.catalogue.write_text(json.dumps({'datasets': [{'id': 'TEST', 'obtained_artifacts': [self.asset], 'prepared_artifact_profiles': [], 'optimization_status': 'incomplete'}]}))
        self.write('data/interim/dataset_selection/latest.json', {'selection': str(self.catalogue.relative_to(self.root))})
        self.policy = {'schema_version': 1, 'default': 'lenient', 'resources': []}
        self.assets = {'schema_version': 1, 'files': []}
        self.request = {'schema_version': 1, 'kind': 'datahunter_license_review', 'dataset_id': 'TEST',
                        'reviewer': 'Human reviewer', 'purpose': 'Local research mapping', 'source_version': 'fixture-v1',
                        'scope': 'Whole fixture file', 'rationale': 'Fixture test approval', 'license_name': 'Fixture grant',
                        'reviewed_on': date.today().isoformat(), 'review_due': (date.today() + timedelta(days=30)).isoformat(),
                        'verdict': 'approve', 'operations': ['local_inference', 'local_embedding'], 'ai_evidence': 'silent',
                        'conditions': ['Research only'], 'assets': [self.asset], 'ai_evidence_note': 'Fixture source clause for the test operation',
                        'evidence': [{'snapshot': 'data/raw/fixture/LICENSE.txt', 'sha256': review.digest(self.root / 'data/raw/fixture/LICENSE.txt'), 'url': 'https://provider.example/license'}],
                        'underlying_rights_confirmed': True, 'conditions_satisfied': True, 'agreements_complete': True, 'whole_artifacts_reviewed': True}
        self.path = self.root / 'request.json'
        self.prepare()

    def write(self, path, value):
        (self.root / path).write_text(json.dumps(value))

    def prepare(self):
        self.write('configs/data_licenses.json', self.policy)
        self.write('configs/data_license_assets.json', self.assets)
        self.request.update(catalogue_sha256=review.digest(self.catalogue), policy_sha256=review.digest(self.root / 'configs/data_licenses.json'), assets_sha256=review.digest(self.root / 'configs/data_license_assets.json'))
        self.path.write_text(json.dumps(self.request))

    def registry(self, mode='lenient'):
        return review.LicenseRegistry(self.root / 'configs/data_licenses.json', self.root / 'configs/data_license_assets.json', root=self.root, mode=mode)

    def registered(self):
        self.policy['resources'] = [{'id': 'original', 'name': 'Fixture', 'license': 'Fixture', 'scope': 'data',
                                     'reason': 'Unreviewed fixture', 'reviewed_on': self.request['reviewed_on'], 'review_due': self.request['review_due'],
                                     'operations': {op: 'unreviewed' for op in review.OPERATIONS}, 'ai_permissions': {}, 'evidence': self.request['evidence'], 'conditions': []}]
        self.assets['files'] = [{**self.asset, 'resource': 'original'}]

    def test_dry_validation_never_changes_runtime_permissions(self):
        before = (self.root / 'configs/data_licenses.json').read_bytes()
        result = review.review(self.path, root=self.root)
        self.assertFalse(result['applied'])
        self.assertEqual(before, (self.root / 'configs/data_licenses.json').read_bytes())
        self.assertFalse((self.root / 'configs/dataset_license_reviews.json').exists())

    def test_registered_decisions_and_revisions_are_logged_to_the_study(self):
        log = self.root / 'docs/research-studies/medscrape/actions.jsonl'
        review.review(self.path, root=self.root)
        self.assertFalse(log.exists())  # validation alone is not an action on the study
        review.review(self.path, True, self.root)
        self.request['verdict'] = 'prohibited'; self.prepare()  # a revised decision against the updated register
        review.review(self.path, True, self.root)
        events = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([e['action'] for e in events], ['licence_review_registered'] * 2)
        self.assertIn('first decision for this study', events[0]['detail'])
        self.assertIn('revises 1 earlier decision', events[1]['detail'])
        self.assertTrue(events[1]['run'].startswith('runs/license_reviews/'))

    def test_exact_manual_approval_allows_lenient_but_silence_blocks_strict(self):
        result = review.review(self.path, True, self.root)
        self.assertEqual(result['status'], 'approved_exact_assets')
        self.assertEqual(self.registry().check([self.asset['sha256']], ['local_inference'])['status'], 'allowed')
        self.assertEqual(self.registry('restrictive').check([self.asset['sha256']], ['local_inference'])['status'], 'blocked')
        self.assertEqual(self.registry().check([self.asset['sha256']], ['remote_inference'])['status'], 'blocked')
        self.assertEqual(json.loads(self.catalogue.read_text())['datasets'][0]['optimization_status'], 'incomplete')

    def test_explicit_source_ai_approval_can_satisfy_strict(self):
        self.request['ai_evidence'] = 'explicit_approval'; self.prepare()
        review.review(self.path, True, self.root)
        self.assertEqual(self.registry('restrictive').check([self.asset['sha256']], ['local_inference'])['status'], 'allowed')

    def test_no_files_means_source_review_not_ai_ready(self):
        self.request['assets'] = []; self.prepare()
        result = review.review(self.path, True, self.root)
        self.assertEqual(result['status'], 'approved_source_pending_identity')
        self.assertEqual(self.registry().check([self.asset['sha256']], ['local_inference'])['status'], 'blocked')

    def test_changed_evidence_blocks_a_previously_approved_input(self):
        review.review(self.path, True, self.root)
        (self.root / 'data/raw/fixture/LICENSE.txt').write_text('Changed notice')
        self.assertEqual(self.registry().check([self.asset['sha256']], ['local_inference'])['status'], 'blocked')

    def test_stale_catalogue_policy_and_altered_asset_are_rejected(self):
        for key in ['catalogue_sha256', 'policy_sha256', 'assets_sha256']:
            altered = {**self.request, key: '0' * 64}
            with self.subTest(key=key), self.assertRaises(ValueError): review.validate(altered, self.root)
        altered = deepcopy(self.request); altered['assets'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'asset_changed'): review.validate(altered, self.root)

    def test_attestations_and_source_ai_prohibition_cannot_be_overridden(self):
        for key in ['underlying_rights_confirmed', 'conditions_satisfied', 'agreements_complete', 'whole_artifacts_reviewed']:
            with self.subTest(key=key), self.assertRaises(ValueError): review.validate({**self.request, key: False}, self.root)
        with self.assertRaisesRegex(ValueError, 'explicit_ai_prohibition'):
            review.validate({**self.request, 'ai_evidence': 'explicit_prohibition'}, self.root)

    def test_existing_explicit_and_conditional_prohibitions_remain_blocked(self):
        self.registered()
        for changes in [{'operations': {'local_inference': 'prohibited'}}, {'ai_permissions': {'local_inference': 'prohibited'}},
                        {'conditional_prohibitions': [{'operations': ['local_inference']}]}]:
            base = deepcopy(self.policy); self.policy['resources'][0].update(changes); self.prepare()
            with self.assertRaisesRegex(ValueError, 'cannot_override_existing_prohibition'): review.validate(self.request, self.root)
            self.policy = base

    def test_existing_full_scope_review_can_clear_unreviewed_resource(self):
        self.registered(); self.prepare()
        review.review(self.path, True, self.root)
        self.assertEqual(self.registry().check([self.asset['sha256']], ['local_inference'])['status'], 'allowed')
        self.assertEqual(self.registry().resources['original']['license'],self.request['license_name'])

    def test_partial_resource_approval_is_rejected(self):
        self.registered(); self.assets['files'].append({'path': 'another-file.csv', 'sha256': 'a' * 64, 'resource': 'original'}); self.prepare()
        with self.assertRaisesRegex(ValueError, 'partial_resource_review'): review.validate(self.request, self.root)

    def test_software_license_cannot_be_promoted_to_data_permission(self):
        self.registered(); self.policy['resources'][0]['scope'] = 'software_only'; self.prepare()
        with self.assertRaisesRegex(ValueError, 'software_permission'): review.validate(self.request, self.root)

    def test_manual_rejection_stops_existing_clearance(self):
        self.registered(); self.policy['resources'][0]['operations']['local_inference'] = 'allowed'
        self.request['verdict'] = 'prohibited'; self.prepare()
        review.review(self.path, True, self.root)
        result = self.registry().check([self.asset['sha256']], ['local_inference'])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['decisions'][0]['state'], 'prohibited')

    def test_manual_review_for_another_study_does_not_change_default_clearance(self):
        self.registered()
        profile={'id':'other-study','name':'Other study','question':'Other clinical mapping?','intended_use':'Research'}
        self.write('configs/research_studies.json',{'schema_version':1,'studies':[profile]})
        self.request['research_study']=profile;self.prepare()
        review.review(self.path,True,self.root)
        default=self.registry().check([self.asset['sha256']],['local_inference'])
        other=review.LicenseRegistry(self.root/'configs/data_licenses.json',self.root/'configs/data_license_assets.json',root=self.root,study_id='other-study').check([self.asset['sha256']],['local_inference'])
        self.assertEqual(default['status'],'blocked');self.assertEqual(other['status'],'allowed')

    def test_pending_review_cannot_remove_an_existing_prohibition(self):
        self.registered();self.policy['resources'][0]['operations']['local_inference']='prohibited'
        self.request['verdict']='needs_clarification';self.prepare();review.review(self.path,True,self.root)
        result=self.registry().check([self.asset['sha256']],['local_inference'])
        self.assertEqual(result['decisions'][0]['state'],'prohibited')

    def test_known_source_ban_cannot_be_evaded_with_an_unregistered_file_hash(self):
        self.registered();self.assets['files']=[]
        self.policy['resources'][0]['catalog_ids']=['TEST']
        self.policy['resources'][0]['operations']['local_inference']='prohibited'
        self.prepare()
        with self.assertRaisesRegex(ValueError,'known_source_prohibition_before_asset_registration'):
            review.validate(self.request,self.root)

    def test_source_refusal_before_acquisition_is_retained_as_a_real_restriction(self):
        asset=self.request['assets'];self.request.update(assets=[],verdict='prohibited');self.prepare()
        review.review(self.path,True,self.root)
        self.policy=json.loads((self.root/'configs/data_licenses.json').read_text())
        self.assets=json.loads((self.root/'configs/data_license_assets.json').read_text())
        self.assertEqual(self.policy['resources'][0]['operations']['local_inference'],'prohibited')
        self.request.update(assets=asset,verdict='approve');self.prepare()
        with self.assertRaisesRegex(ValueError,'known_source_prohibition_before_asset_registration'):
            review.validate(self.request,self.root)

    def test_publisher_ai_ban_cannot_be_laundered_through_another_study(self):
        self.registered();self.assets['files']=[]
        self.policy['resources'][0].update(catalog_ids=['TEST'],study_id='other-study',ai_permissions={'local_inference':'prohibited'})
        self.prepare()
        with self.assertRaisesRegex(ValueError,'known_source_ai_prohibition'):
            review.validate(self.request,self.root)

if __name__ == '__main__': unittest.main()
