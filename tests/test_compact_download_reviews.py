"""Shared approval evidence: lossless compaction of a download-review file and the sharing writer; offline."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

CODE_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('compact_download_reviews', CODE_ROOT / 'Scripts/compact_download_reviews.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
import public_resources  # noqa: E402  (on the path once the script is loaded)


class CompactDownloadReviewsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); (self.root / 'configs').mkdir()
        self.release = [{'path': f'notices/manifest_{i}.json', 'sha256': f'{i:064x}'} for i in range(20)]
        self.other = [{'path': 'notices/other.json', 'sha256': 'f' * 64}]
        approvals = [{'id': f'file_{i}', 'url': f'https://provider.example/{i}', 'metadata_evidence': list(self.release)} for i in range(50)]
        approvals.append({'id': 'manual', 'url': 'https://provider.example/manual', 'metadata_evidence': self.other})
        self.reviews = {'schema_version': 1, 'reviews': [{'id': 'DEMO', 'metadata_evidence': self.release}], 'approved_artifacts': approvals}
        (self.root / 'configs/dataset_download_reviews.json').write_text(json.dumps(self.reviews, indent=2))
        (self.root / 'study.json').write_text(json.dumps({'id': 'demo-study'}))

    def test_compaction_is_lossless_smaller_and_logged(self):
        result = module.main([], root=self.root)
        after = json.loads((self.root / 'configs/dataset_download_reviews.json').read_text())
        self.assertLess(result['bytes_after'], result['bytes_before'] / 5)
        self.assertEqual((result['approvals'], result['evidence_sets'], after['schema_version']), (51, 2, 2))
        for before, now in zip(self.reviews['approved_artifacts'], after['approved_artifacts']):
            self.assertNotIn('metadata_evidence', now)
            self.assertEqual(public_resources.approval_evidence(after, now), before['metadata_evidence'])
            self.assertEqual({k: v for k, v in now.items() if k != 'metadata_evidence_set'}, {k: v for k, v in before.items() if k != 'metadata_evidence'})
        self.assertEqual(after['reviews'], self.reviews['reviews'], 'the dataset review entries are unchanged')
        log = [json.loads(line) for line in (self.root / 'actions.jsonl').read_text().splitlines()]
        self.assertEqual((log[-1]['action'], log[-1]['source']), ('note', 'compact_download_reviews.py'))
        again = module.main([], root=self.root)
        self.assertEqual(again['bytes_after'], again['bytes_before'], 'a second run changes nothing')
        self.assertEqual(len((self.root / 'actions.jsonl').read_text().splitlines()), 1, 'and logs nothing')

    def test_dry_run_writes_nothing(self):
        before = (self.root / 'configs/dataset_download_reviews.json').read_bytes()
        result = module.main(['--dry-run'], root=self.root)
        self.assertNotIn('bytes_after', result)
        self.assertEqual((self.root / 'configs/dataset_download_reviews.json').read_bytes(), before)
        self.assertFalse((self.root / 'actions.jsonl').exists())

    def test_sets_no_approval_names_are_dropped_and_a_changed_set_is_kept_for_the_gate_to_refuse(self):
        reviews = public_resources.share_evidence(json.loads(json.dumps(self.reviews)))
        manual = reviews['approved_artifacts'].pop()  # withdrawn
        public_resources.share_evidence(reviews)
        self.assertNotIn(manual['metadata_evidence_set'], reviews['evidence_sets'])
        key = reviews['approved_artifacts'][0]['metadata_evidence_set']
        reviews['evidence_sets'][key] = self.other
        public_resources.share_evidence(reviews)
        with self.assertRaisesRegex(ValueError, 'evidence_set_missing_or_changed'):
            public_resources.approval_evidence(reviews, reviews['approved_artifacts'][0])
        reviews['approved_artifacts'].append({'id': 'new', 'metadata_evidence': list(self.release)})
        public_resources.share_evidence(reviews)
        self.assertEqual(public_resources.approval_evidence(reviews, reviews['approved_artifacts'][0]), self.release, 'a new approval with the same list restores its content')


if __name__ == '__main__':
    unittest.main()
