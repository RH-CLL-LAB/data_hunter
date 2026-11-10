"""The explicit unresolved-rights download option widens acquisition only: open routes, flagged approvals, no permission."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

CODE_ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, CODE_ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = load('download_agent_unresolved', 'Scripts/run_download_agent.py')
acquisition = load('acquisition_unresolved', 'Scripts/acquire_research_resources.py')


class InstructionTests(unittest.TestCase):
    def setUp(self):
        self.spec = {'schema_version': 1, 'kind': 'datahunter_download_instructions', 'catalogue_sha256': 'abc', 'dataset_ids': ['A'], 'license_mode': 'lenient',
                     'download_scope': 'minimal', 'max_bytes_per_artifact': 1024, 'max_total_bytes': 2048, 'technical_risk_acknowledgements': []}

    def test_option_is_an_explicit_boolean_and_needs_a_download_scope(self):
        agent.validate_instructions({**self.spec, 'acquire_unresolved_rights': True}, 'abc')
        agent.validate_instructions({**self.spec, 'acquire_unresolved_rights': False, 'download_scope': 'review_only', 'download_requested': False}, 'abc')
        for changes in [{'acquire_unresolved_rights': 'yes'}, {'acquire_unresolved_rights': True, 'download_scope': 'review_only', 'download_requested': False}]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'acquire_unresolved_rights'):
                agent.validate_instructions({**self.spec, **changes}, 'abc')
        agent.validate_instructions({**self.spec, 'download_scope': 'full', 'direct_holdings_for_acknowledged_risks': True}, 'abc')
        for changes in [{'direct_holdings_for_acknowledged_risks': True}, {'direct_holdings_for_acknowledged_risks': 'true', 'download_scope': 'full'}]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'direct_holdings'):
                agent.validate_instructions({**self.spec, **changes}, 'abc')

    def test_csv_round_trips_the_option(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'task.csv'
            header = ['id', 'schema_version', 'kind', 'catalogue_sha256', 'license_mode', 'max_bytes_per_artifact', 'max_total_bytes', 'technical_risk_acknowledgements', 'download_scope', 'acquire_unresolved_rights']
            path.write_text(','.join(header) + '\nA,1,datahunter_download_instructions,abc,lenient,1024,2048,[],minimal,true\n')
            self.assertIs(agent.read_instructions(path)['acquire_unresolved_rights'], True)
            path.write_text(','.join(header[:-1]) + '\nA,1,datahunter_download_instructions,abc,lenient,1024,2048,[],minimal\n')
            self.assertNotIn('acquire_unresolved_rights', agent.read_instructions(path))


class GateTests(unittest.TestCase):
    """The acquisition gate yields only when the caller passes the option AND the exact approval carries the flag."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / 'configs').mkdir()
        self.payload = b'open data, licence unresolved'
        self.item = {'id': 'open_table', 'kind': 'dataset', 'acquisition': 'direct', 'url': 'https://provider.example/open.csv', 'path': 'datasets/open/open.csv', 'format': 'csv', 'max_bytes': 4096}
        notice = self.root / 'notice.txt'
        notice.write_text('terms page retained')
        self.approval = {**self.item, 'decision': 'approve_minimal_subset', 'use_case': 'open-access table', 'file_selection_reason': 'complete release',
                         'metadata_evidence': [{'path': 'notice.txt', 'sha256': hashlib.sha256(notice.read_bytes()).hexdigest()}],
                         'checks': dict.fromkeys(['clinical_content', 'subject_and_grain', 'value_units_codes', 'time_semantics', 'format_and_size', 'duplicate_overlap', 'test_gap', 'access'], True)}
        self.blocked = {'status': 'blocked', 'reason': 'exact_artifact_license_binding_missing', 'mode': 'lenient'}

    def write_review(self, flagged):
        (self.root / 'configs/dataset_download_reviews.json').write_text(json.dumps({'reviews': [], 'approved_artifacts': [{**self.approval, **({'rights_unresolved_download': True} if flagged else {})}]}))

    def transfer(self, **kwargs):
        def fake(command, **_):
            Path(command[command.index('--output') + 1]).write_bytes(self.payload)
            return type('Run', (), {'returncode': 0, 'stderr': ''})()
        with patch.object(acquisition, 'ROOT', self.root), patch.object(acquisition, 'transfer_license_preflight', return_value=self.blocked), patch.object(acquisition.subprocess, 'run', fake):
            return acquisition.acquire(self.item, {}, base=self.root / 'stage', execute=True, **kwargs)

    def test_blocked_without_the_option_or_without_the_flagged_approval(self):
        self.write_review(flagged=True)
        with self.assertRaisesRegex(ValueError, 'dataset_license_required_before_transfer'):
            self.transfer()
        self.write_review(flagged=False)
        with self.assertRaisesRegex(ValueError, 'dataset_license_required_before_transfer'):
            self.transfer(allow_unresolved_rights=True)
        self.assertFalse((self.root / 'stage/datasets/open/open.csv').exists())

    def test_allowed_with_option_and_flag_but_never_over_an_explicit_prohibition(self):
        self.write_review(flagged=True)
        record = self.transfer(allow_unresolved_rights=True)
        self.assertEqual(record['state'], 'downloaded_verified')
        self.assertEqual(record['hash_authority'], 'local_first_acquisition_SHA256_not_provider_checksum')
        self.assertEqual((self.root / 'stage/datasets/open/open.csv').read_bytes(), self.payload)
        prohibition = {**self.blocked, 'reason': 'explicit_source_ai_prohibition_before_acquisition'}
        with patch.object(acquisition, 'ROOT', self.root), patch.object(acquisition, 'transfer_license_preflight', return_value=prohibition), \
                patch.object(acquisition.subprocess, 'run', side_effect=AssertionError('must not transfer')):
            with self.assertRaisesRegex(ValueError, 'dataset_license_required_before_transfer'):
                acquisition.acquire(self.item, {}, base=self.root / 'stage-prohibited', execute=True, allow_unresolved_rights=True)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.job = {'id': 'open_table', 'acquisition': 'direct', 'kind': 'dataset', 'format': 'json', 'url': 'https://provider.example/open.json', 'path': 'open.json', 'max_bytes': 1024, 'provider_sha256': 'f' * 64, 'expected_bytes': 2}
        self.spec = {'max_bytes_per_artifact': 1024, 'max_total_bytes': 2048, 'license_mode': 'lenient', 'acquire_unresolved_rights': True}
        self.events = []
        (self.root / 'configs').mkdir()

    def process(self, review):
        with patch.object(agent.acquisition, 'require_download_review', return_value=review), \
                patch.object(agent.acquisition, 'acquire', return_value={'id': 'open_table', 'path': 'open.json', 'url': self.job['url'], 'bytes': 2, 'sha256': 'f' * 64, 'hash_authority': 'provider_SHA256', 'acquired_at': 'now', 'state': 'downloaded_verified'}):
            return agent.process_job(self.job, {}, self.root / 'stage', self.root / 'original', set(), True, self.spec, lambda *a: self.events.append(a), [0], {'status': 'blocked', 'mode': 'lenient'})

    def test_blocked_licence_proceeds_only_with_option_and_flag_and_is_labelled(self):
        result = self.process({'basis': 'metadata_reviewed_minimal_subset', 'rights_unresolved_download': True})
        self.assertEqual(result['status'], 'downloaded_rights_unresolved')
        self.assertTrue(result['rights_unresolved'])
        self.assertIn(agent.UNRESOLVED_RIGHTS_WARNING, result['warnings'])
        self.assertEqual(self.process({'basis': 'metadata_reviewed_minimal_subset'})['status'], 'blocked_license')
        self.spec['acquire_unresolved_rights'] = False
        self.assertEqual(self.process({'basis': 'metadata_reviewed_minimal_subset', 'rights_unresolved_download': True})['status'], 'blocked_license')


if __name__ == '__main__':
    unittest.main()
