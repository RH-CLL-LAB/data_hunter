"""Quarantine listing, request validation and reviewed release: offline fixtures, no network."""
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

CODE_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review_quarantine', CODE_ROOT / 'Scripts/review_quarantine.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1) if not isinstance(value, (str, bytes)) else value) if isinstance(value, str) or not isinstance(value, bytes) else path.write_bytes(value)


class QuarantineFixture(unittest.TestCase):
    """A study workspace with one approved job delivered into quarantine by a download-agent run."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.payload = b'subject,value,unit\n1,2,mg\n'
        self.run = '20260101T000000000000Z'
        self.job = {'id': 'table_csv', 'kind': 'dataset', 'acquisition': 'direct', 'url': 'https://provider.example/table.csv', 'path': 'datasets/demo/table.csv', 'format': 'csv', 'max_bytes': 4096}
        today = date.today().isoformat()
        write(self.root / 'study.json', {'schema_version': 1, 'id': 'demo-study'})
        write(self.root / 'configs/research_studies.json', {'schema_version': 1, 'default_study': 'demo-study', 'studies': [
            {'id': 'demo-study', 'name': 'Demo', 'question': 'q', 'intended_use': 'u', 'population': '', 'targets': '', 'inclusion': '', 'exclusion': '',
             'dataset_ids': [], 'dataset_annotations': {}, 'study_annotations': {}}]})
        write(self.root / 'configs/research_resources.json', {'schema_version': 1, 'scope': 'fixture', 'resources': [self.job]})
        write(self.root / 'configs/research_resources.lock.json', {'schema_version': 1, 'scope': 'fixture', 'files': []})
        write(self.root / 'configs/dataset_study_usage.json', {'datasets': [{'id': 'DEMO', 'paths': [], 'completeness': 'not_obtained', 'missing_components': ['x'], 'next_action': 'y'}]})
        write(self.root / 'configs/dataset_download_reviews.json', {'reviews': [{'id': 'DEMO', 'resource_aliases': ['table_csv'], 'full_release': {'files': 1}}],
                                                                       'approved_artifacts': [{**self.job, 'decision': 'approve_minimal_subset', 'download_license_resources': ['demo_cc_by']}]})
        write(self.root / 'configs/data_license_assets.json', {'schema_version': 1, 'as_of': today, 'files': []})
        write(self.root / 'data/acquisitions/acquisition-report.json', {'checked_at': 'x', 'manifest_sha256': 'm', 'results': [], 'counts': {}})
        staged = self.root / 'data/quarantine/download_agent' / self.run / self.job['path']
        staged.parent.mkdir(parents=True)
        staged.write_bytes(self.payload)
        self.record = {'id': 'table_csv', 'path': self.job['path'], 'url': self.job['url'], 'bytes': len(self.payload), 'sha256': sha(self.payload),
                       'hash_authority': 'local_first_acquisition_SHA256_not_provider_checksum', 'acquired_at': '2026-01-01T00:00:00+00:00'}
        write(self.root / 'runs/downloads' / self.run / 'report.json', {'unique_artifacts': [{'id': 'table_csv', 'status': 'downloaded_quarantined',
                                                                                                'technical_risks': ['missing_provider_checksum'], 'record': self.record}]})

    def request(self, action='release', **changes):
        latest = json.loads((self.root / 'data/interim/quarantine_review/latest.json').read_text())
        return {'schema_version': 1, 'kind': 'datahunter_quarantine_release', 'listing_sha256': latest['listing_sha256'], 'reviewed': True,
                'items': [{'run': self.run, 'id': 'table_csv', 'job_path': self.job['path'], 'action': action}], **changes}


class RegistryRefreshTests(QuarantineFixture):
    def test_release_files_held_elsewhere_withdrawn_or_pending_are_counted_from_the_last_resolution(self):
        write(self.root / 'configs/research_resources.lock.json', {'schema_version': 1, 'scope': 'fixture', 'files': [self.record]})
        reviews = json.loads((self.root / 'configs/dataset_download_reviews.json').read_text())

        def refresh(full, aliases=('table_csv',)):
            reviews['reviews'][0].update(resource_aliases=list(aliases), full_release=full)
            write(self.root / 'configs/dataset_download_reviews.json', reviews)
            self.assertEqual(module.refresh_dataset_entries(self.root, {'DEMO'}), ['DEMO'])
            return json.loads((self.root / 'configs/dataset_study_usage.json').read_text())['datasets'][0]
        entry = refresh({'files': 2, 'previously_held': 1, 'jobs_added': 1})  # one release file was held under another dataset's folder when resolved
        self.assertEqual(entry['completeness'], 'complete_public_release'); self.assertTrue(entry['next_action'].startswith('Complete release held (2 files'))
        entry = refresh({'files': 3, 'previously_held': 1, 'jobs_added': 2}, aliases=('table_csv', 'other_csv'))  # one registered job is still pending
        self.assertEqual(entry['completeness'], 'partial_release_2_of_3_files_held')
        entry = refresh({'files': 3, 'previously_held': 0, 'jobs_added': 3, 'withdrawn_unheld_jobs': 2})  # two size-blocked jobs were withdrawn
        self.assertEqual(entry['completeness'], 'partial_release_1_of_3_files_held')
        entry = refresh({'files': 4, 'previously_held': 4, 'jobs_added': 0, 'withdrawn_unheld_jobs': 0, 'complete': True})  # confirmed complete; aliases from an older release add nothing
        self.assertEqual(entry['completeness'], 'complete_public_release')


class InventoryTests(QuarantineFixture):
    def test_delivery_not_held_is_pending_and_duplicates_or_conflicts_are_named(self):
        listing = module.inventory(self.root)
        self.assertEqual([i['state'] for i in listing['items']], ['pending_review'])
        self.assertEqual(listing['items'][0]['dataset_ids'], ['DEMO'])
        self.assertEqual(listing['items'][0]['license_resources'], ['demo_cc_by'])
        self.assertIn('No provider checksum', listing['items'][0]['reason'])
        self.assertIn('not held yet', listing['items'][0]['reason'])
        self.assertTrue((self.root / 'data/interim/quarantine_review/latest.json').exists())
        lock = self.root / 'configs/research_resources.lock.json'
        write(lock, {'schema_version': 1, 'files': [self.record]})
        self.assertEqual(module.inventory(self.root, write=False)['items'][0]['state'], 'superseded_duplicate')
        write(lock, {'schema_version': 1, 'files': [{**self.record, 'sha256': 'f' * 64}]})
        self.assertEqual(module.inventory(self.root, write=False)['items'][0]['state'], 'identity_changed')

    def test_orphans_missing_and_changed_files_are_reported_not_released(self):
        (self.root / 'data/quarantine/download_agent' / self.run / 'datasets/demo/stray.bin').write_bytes(b'?')
        states = {i['job_path']: i['state'] for i in module.inventory(self.root, write=False)['items']}
        self.assertEqual(states['datasets/demo/stray.bin'], 'orphan')
        (self.root / 'data/quarantine/download_agent' / self.run / self.job['path']).write_bytes(b'tampered')
        self.assertEqual({i['state'] for i in module.inventory(self.root, write=False)['items'] if i['id'] == 'table_csv'}, {'staged_bytes_changed'})
        (self.root / 'data/quarantine/download_agent' / self.run / self.job['path']).unlink()
        self.assertIn('missing_file', {i['state'] for i in module.inventory(self.root, write=False)['items']})


class RequestTests(QuarantineFixture):
    def test_requests_must_match_the_current_listing_and_allowed_actions(self):
        module.inventory(self.root)
        module.validate_request(self.request(), self.root)
        for changes, message in [({'listing_sha256': 'a' * 64}, 'listing_changed'), ({'reviewed': False}, 'reviewer_confirmation'),
                                 ({'items': [{'run': self.run, 'id': 'table_csv', 'job_path': self.job['path'], 'action': 'discard_superseded'}]}, 'action_not_allowed'),
                                 ({'items': [{'run': self.run, 'id': 'other', 'job_path': 'x', 'action': 'release'}]}, 'not_in_listing'),
                                 ({'kind': 'datahunter_download_instructions'}, 'unsupported')]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                module.validate_request(self.request(**changes), self.root)
        write(self.root / 'runs/downloads' / self.run / 'report.json', {'unique_artifacts': []})
        module.inventory(self.root)  # the listing changed: an old request no longer applies
        with self.assertRaisesRegex(ValueError, 'not_in_listing'):
            module.validate_request(self.request(), self.root)

    def test_study_must_be_registered_with_the_same_brief(self):
        module.inventory(self.root)
        study = {'id': 'demo-study', 'name': 'Demo', 'question': 'q', 'intended_use': 'u'}
        module.validate_request(self.request(research_study=study), self.root)
        with self.assertRaisesRegex(ValueError, 'register_current_research_study'):
            module.validate_request(self.request(research_study={**study, 'question': 'changed'}), self.root)


class ReleaseTests(QuarantineFixture):
    def test_dry_run_plans_and_execute_releases_locks_binds_and_logs(self):
        module.inventory(self.root)
        with patch.object(module.subprocess, 'run', side_effect=AssertionError('no network in a release')):
            planned = module.apply(self.request(), self.root, execute=False)
            self.assertEqual(planned['counts'], {'release_planned': 1})
            self.assertTrue((self.root / 'data/quarantine/download_agent' / self.run / self.job['path']).exists())
            result = module.apply(self.request(), self.root, execute=True)
        self.assertEqual(result['counts'], {'released_locked': 1})
        held = self.root / 'data/acquisitions' / self.job['path']
        self.assertEqual(held.read_bytes(), self.payload)
        self.assertFalse((self.root / 'data/quarantine/download_agent' / self.run).exists(), 'emptied run folder is removed')
        lock = json.loads((self.root / 'configs/research_resources.lock.json').read_text())['files']
        self.assertEqual([(f['id'], f['sha256'], f['hash_authority']) for f in lock], [('table_csv', sha(self.payload), 'local_first_acquisition_SHA256_not_provider_checksum')])
        report = json.loads((self.root / 'data/acquisitions/acquisition-report.json').read_text())
        self.assertEqual(report['results'][0]['released_from_quarantine']['run'], self.run)
        assets = json.loads((self.root / 'configs/data_license_assets.json').read_text())['files']
        self.assertEqual(assets, [{'path': 'data/acquisitions/' + self.job['path'], 'sha256': sha(self.payload), 'resource': 'demo_cc_by'}])
        log = [json.loads(l) for l in (self.root / 'actions.jsonl').read_text().splitlines()]
        self.assertEqual(log[-1]['action'], 'quarantine_release')
        self.assertIn('grant no AI-use permission', log[-1]['detail'])
        self.assertEqual(result['quarantine_after'], {})
        self.assertFalse(result['ai_use_authorized_by_release'])
        entry = json.loads((self.root / 'configs/dataset_study_usage.json').read_text())['datasets'][0]
        self.assertEqual((entry['completeness'], entry['paths'], entry['missing_components'], entry['audit_state']), ('complete_public_release', ['data/acquisitions/datasets/demo/'], [], 'acquired_locked_verified'))

    def test_second_download_must_match_and_changed_staged_bytes_block(self):
        module.inventory(self.root)
        with patch.object(module, 'second_download', return_value='0' * 64):
            result = module.apply(self.request(), self.root, execute=True, reverify=True)
        self.assertEqual(result['outcomes'][0]['status'], 'blocked')
        self.assertIn('second_download_differs', result['outcomes'][0]['problem'])
        self.assertTrue((self.root / 'data/quarantine/download_agent' / self.run / self.job['path']).exists(), 'blocked release leaves the staged file in place')
        module.inventory(self.root)
        request = self.request()
        (self.root / 'data/quarantine/download_agent' / self.run / self.job['path']).write_bytes(b'tampered after listing')
        result = module.apply(request, self.root, execute=True)
        self.assertIn('staged_file_changed_since_listing', result['outcomes'][0]['problem'])
        self.assertEqual(json.loads((self.root / 'configs/research_resources.lock.json').read_text())['files'], [])

    def test_discard_deletes_only_byte_identical_duplicates_of_held_files(self):
        held = self.root / 'data/acquisitions' / self.job['path']
        held.parent.mkdir(parents=True)
        held.write_bytes(self.payload)
        write(self.root / 'configs/research_resources.lock.json', {'schema_version': 1, 'files': [self.record]})
        module.inventory(self.root)
        result = module.apply(self.request('discard_superseded'), self.root, execute=True)
        self.assertEqual(result['counts'], {'discarded_duplicate': 1})
        self.assertFalse((self.root / 'data/quarantine/download_agent' / self.run / self.job['path']).exists())
        self.assertEqual(held.read_bytes(), self.payload)
        # A duplicate whose held copy changed is kept.
        staged = self.root / 'data/quarantine/download_agent' / self.run / self.job['path']
        staged.parent.mkdir(parents=True)
        staged.write_bytes(self.payload)
        held.write_bytes(b'held copy changed')
        module.inventory(self.root)
        result = module.apply(self.request('discard_superseded'), self.root, execute=True)
        self.assertEqual(result['outcomes'][0]['status'], 'blocked')
        self.assertTrue(staged.exists())


if __name__ == '__main__':
    unittest.main()
