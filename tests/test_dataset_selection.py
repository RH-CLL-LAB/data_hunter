"""Selection facts must distinguish missing data, shared sources and disease fit."""
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
spec = importlib.util.spec_from_file_location('dataset_selection', CODE_ROOT/'Scripts/document_dataset_selection.py')
selection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selection)


class DatasetSelectionTests(unittest.TestCase):
    def test_unknown_size_is_not_zero(self):
        self.assertNotEqual(selection.size(None), selection.size(0))
        self.assertEqual(selection.size(0), '0 B')
        self.assertIn('not held', selection.size(None))

    def test_one_archive_member_study_does_not_claim_other_studies(self):
        dataset = {'id': 'GDC_CLARK', 'obtained_artifacts': [{
            'path': 'shared.tar.gz', 'structure': {'state': 'passed_container',
            'contained_tables': [{'member': 'GDC/clark/source.csv', 'rows': 10, 'columns': 5, 'state': 'passed_structure'},
                                 {'member': 'GDC/krug/source.csv', 'rows': 20, 'columns': 8, 'state': 'passed_structure'}]}}]}
        result = selection.profiles(dataset)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['records'], 10)


    def test_unreviewed_pending_source_remains_unassessed_and_on_hold(self):
        policy = json.loads((ROOT / 'configs/dataset_selection.json').read_text())
        result = selection.judgement({'id': 'UNKNOWN_FUTURE_SOURCE', 'category': 'pending_access',
                                      'optimization_status': 'incomplete'}, policy)
        self.assertEqual(result['workflow_relevance'], 'unassessed')
        self.assertEqual(result['action_priority'], 'P3 / hold')

    def test_action_assignment_is_unambiguous_and_covers_only_registered_ids(self):
        policy = json.loads((ROOT / 'configs/dataset_selection.json').read_text())
        registry = json.loads((ROOT / 'configs/dataset_study_usage.json').read_text())
        ids = {d['id'] for d in registry['datasets']}
        assigned = [ident for group in policy['priority_groups'] for ident in group['ids']]
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertTrue(set(assigned) <= ids)
        self.assertTrue(set(policy['relevance_overrides']) <= ids)


if __name__ == '__main__':
    unittest.main()
