"""The status document derives one state and one action per dataset from the registries; offline fixtures."""
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

CODE_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('report_dataset_status', CODE_ROOT / 'Scripts/report_dataset_status.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1) if not isinstance(value, str) else value)


def dataset(id, **values):
    base = {'id': id, 'name': id.title(), 'completeness': 'not_obtained', 'access_route': 'public_no_registration', 'registration_url': None, 'missing_components': [],
            'next_action': 'Next step for ' + id, 'category': 'clinical_sources', 'associated_download_files': 0, 'associated_download_bytes': None, 'source_locators': [], 'source_urls': []}
    return {**base, **values}


class StatusReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        today = date.today()
        write(self.root / 'study.json', {'schema_version': 1, 'id': 'demo-study'})
        write(self.root / 'configs/research_studies.json', {'schema_version': 1, 'default_study': 'demo-study', 'studies': [
            {'id': 'demo-study', 'name': 'Demo study', 'question': 'q', 'intended_use': 'u', 'population': '', 'targets': '', 'inclusion': '', 'exclusion': '',
             'dataset_ids': [], 'dataset_annotations': {}, 'study_annotations': {}}]})
        self.datasets = [
            dataset('OPEN_DONE', completeness='complete_public_release', associated_download_files=3, associated_download_bytes=300, source_locators=['data/acquisitions/datasets/open/']),
            dataset('HUMAN_DONE', completeness='complete_public_release', associated_download_files=1, associated_download_bytes=10),
            dataset('PART', completeness='partial_release_two_of_four', associated_download_files=2, associated_download_bytes=20, access_route='public_mixed_sources',
                    missing_components=['Two sets are Kaggle-hosted.']),
            dataset('CRED', access_route='registration_credentialed_access', registration_url='https://provider.example/credentialing/'),
            dataset('TICK', next_action='Blocked pending acknowledgement', missing_components=['Provider checksums do not exist for this release; the instructions accept no technical-risk acknowledgement.']),
            dataset('TERMS', access_route='public_pending_file_level_review'),
            dataset('NOWAY', access_route='no_public_access_route', category='excluded'),
            dataset('CODE', completeness='complete_public_release', associated_download_files=5, associated_download_bytes=50, category='standards_and_methods'),
        ]
        selection = self.root / 'data/interim/dataset_selection/20260101T000000000000Z/selection.json'
        write(selection, {'datasets': self.datasets})
        write(self.root / 'data/interim/dataset_selection/latest.json', {'selection': str(selection.relative_to(self.root)), 'selection_sha256': hashlib.sha256(selection.read_bytes()).hexdigest()})
        write(self.root / 'configs/dataset_study_usage.json', {'datasets': [{'id': d['id']} for d in self.datasets]})
        write(self.root / 'configs/dataset_download_reviews.json', {'reviews': [{'id': 'PART', 'resource_aliases': ['p1', 'p2'], 'full_release': {'files': 4}},
                                                                                {'id': 'OPEN_DONE', 'resource_aliases': ['o1', 'o2', 'o3'], 'full_release': {'files': 3}}], 'approved_artifacts': []})
        write(self.root / 'configs/research_resources.json', {'schema_version': 1, 'resources': []})
        write(self.root / 'configs/research_resources.lock.json', {'schema_version': 1, 'files': [{'id': 'o1', 'path': 'datasets/open/a', 'sha256': 'a' * 64, 'bytes': 100}]})
        notice = self.root / 'notice.txt'
        notice.write_text('CC BY 4.0')
        def licence(id, catalog, **changes):
            return {'id': id, 'name': id, 'license': 'CC-BY-4.0', 'scope': 'data', 'reviewed_on': today.isoformat(), 'review_due': (today + timedelta(days=30)).isoformat(),
                    'study_id': 'demo-study', 'reason': 'ok', 'conditions': [], 'sources': [], 'catalog_ids': catalog,
                    'operations': {'local_inference': 'allowed', 'local_embedding': 'allowed'}, 'ai_permissions': {},
                    'evidence': [{'snapshot': 'notice.txt', 'sha256': hashlib.sha256(notice.read_bytes()).hexdigest()}], **changes}
        write(self.root / 'configs/data_licenses.json', {'schema_version': 1, 'default': 'lenient', 'resources': [
            licence('open_cc_by', ['OPEN_DONE']), licence('human_cc_by', ['HUMAN_DONE']), licence('part_cc_by', ['PART']),
            licence('cred_dua', ['CRED'], operations={'local_inference': 'unreviewed', 'local_embedding': 'unreviewed'}),
            licence('code_mit', ['CODE'], scope='software_only', operations={'local_inference': 'unreviewed', 'local_embedding': 'unreviewed'})]})
        write(self.root / 'configs/dataset_license_reviews.json', {'reviews': [{'dataset_id': 'HUMAN_DONE', 'status': 'approved_exact_assets', 'applied': True}]})
        write(self.root / 'configs/data_license_assets.json', {'schema_version': 1, 'files': []})

    def test_states_and_actions_follow_the_registries(self):
        data = module.collect(self.root)
        rows = {r['id']: r for r in (module.classify(d, data) for d in self.datasets)}
        self.assertEqual({k: v['state'] for k, v in rows.items()},
                         {'OPEN_DONE': 'AUTOMATIC', 'HUMAN_DONE': 'AUTOMATIC', 'PART': 'PARTIAL', 'CRED': 'ACTION NEEDED', 'TICK': 'ACTION NEEDED', 'TERMS': 'ACTION NEEDED', 'NOWAY': 'NO ROUTE', 'CODE': 'AUTOMATIC'})
        self.assertIn('confirm the licence record open_cc_by', rows['OPEN_DONE']['actions'][0], 'agent-recorded licence awaits a human confirmation')
        self.assertEqual(rows['HUMAN_DONE']['actions'], ['nothing'], 'a human-reviewed licence needs nothing')
        self.assertEqual(rows['CODE']['actions'], ['nothing (software release; never an AI data input)'])
        self.assertEqual(rows['CRED']['cause'], 'agreement')
        self.assertIn('https://provider.example/credentialing/', rows['CRED']['actions'][0])
        self.assertEqual(rows['TICK']['cause'], 'tick')
        self.assertIn('missing provider checksum', rows['TICK']['actions'][0])
        self.assertEqual(rows['TERMS']['cause'], 'licence')
        self.assertEqual(rows['NOWAY']['cause'], 'route')
        self.assertEqual((rows['PART']['held'], rows['PART']['release']), (2, 4))
        self.assertTrue(any(a.startswith('Remaining part: Two sets') for a in rows['PART']['actions']))
        self.assertEqual(rows['CRED']['licence']['unreviewed'], ['cred_dua'])

    def test_document_has_the_glance_table_groups_quarantine_and_confirmations(self):
        doc, rows, counts = module.write_document(self.root)
        text = doc.read_text()
        self.assertEqual(counts, {'AUTOMATIC': 3, 'PARTIAL': 1, 'ACTION NEEDED': 3, 'NO ROUTE': 1})
        self.assertIn('| OPEN_DONE | **AUTOMATIC** | 3 / 3 files |', text)
        self.assertIn('| PART | **PARTIAL** | 2 / 4 files |', text)
        self.assertIn('## Needs an access agreement or registration only you can complete', text)
        self.assertIn('## Needs one tick from you: a technical exception', text)
        self.assertIn('The quarantine folder is empty', text)
        self.assertIn('| OPEN_DONE | `open_cc_by` |', text)
        self.assertNotIn('| HUMAN_DONE | `human_cc_by` |', text)
        self.assertIn('nothing here grants data or AI-use permission', text)
        self.assertIn('DATA_HUNTER_STUDY_ID=demo-study', text)

    def test_dated_report_is_tracked_and_registered_with_the_study(self):
        doc, rows, counts = module.write_document(self.root)
        sys.path.insert(0, str(CODE_ROOT / 'Scripts'))
        import register_research_study
        with patch.object(register_research_study, 'set_active', lambda *_: None):  # the fixture study has no folder under studies/
            target = module.save_report(self.root, doc)
        self.assertEqual(target.read_text(), doc.read_text().replace('](DATASET_SELECTION.md#', '](../docs/DATASET_SELECTION.md#'), 'the saved copy keeps the text; its sibling links point at docs/ so the site build resolves them from reports/')
        self.assertNotIn('](DATASET_SELECTION.md#', target.read_text())
        self.assertIn('](../docs/DATASET_SELECTION.md#', target.read_text())
        registry = json.loads((self.root / 'configs/research_studies.json').read_text())
        reports = registry['studies'][0]['reports']
        self.assertEqual([r['path'] for r in reports], [str(target.relative_to(self.root))])
        self.assertEqual(reports[0]['sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        log = [json.loads(l)['action'] for l in (self.root / 'actions.jsonl').read_text().splitlines()]
        self.assertIn('report_added', log)


if __name__ == '__main__':
    unittest.main()
