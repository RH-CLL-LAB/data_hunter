"""Readiness must not turn metadata, source damage or development into gold."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dataset_workspace', ROOT / 'Scripts/validate_dataset_workspace.py')
workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace)


class WorkspaceValidationTests(unittest.TestCase):
    def test_development_success_never_becomes_independent_benchmark(self):
        dev, benchmark = workspace.readiness('clinical_sources', True, [{'status': 'ready_scoped'}])
        self.assertEqual(dev, 'ready_scoped')
        self.assertNotEqual(benchmark, 'ready_scoped')
        self.assertIn('development_only', benchmark)

    def test_metadata_and_missing_participants_remain_incomplete(self):
        self.assertEqual(workspace.readiness('pending_access', True, [])[0], 'incomplete')
        self.assertEqual(workspace.readiness('metadata_only', True, [])[0], 'reference_only')

    def test_fresh_identity_failure_blocks_ready_scope(self):
        self.assertEqual(workspace.readiness('clinical_sources', True,
                         [{'status': 'ready_scoped'}], True), ('blocked_integrity', 'blocked_integrity'))

    def test_ragged_and_provider_text_damage_are_preserved(self):
        original = 'name,description\na,damaged\ufffdtext\nb\n'
        profile = workspace.table_profile(io.StringIO(original))
        self.assertEqual(profile['provider_replacement_characters'], 1)
        self.assertEqual(profile['ragged_rows'], 1)
        self.assertEqual(profile['state'], 'needs_review')
        self.assertIn('\ufffd', original)

    def test_sectioned_profile_is_not_called_malformed_flat_table(self):
        profile = workspace.table_profile(io.StringIO('Section\nfield,count\nx,2\n'), sectioned=True)
        self.assertEqual(profile['issues'], ['sectioned_profile_not_rectangular_input'])

    def test_shared_directory_is_narrowed_to_declared_dictionary(self):
        files = [{'path': 'data/references/ADRD.csv'}, {'path': 'data/references/Eye.csv'}]
        _, matched = workspace.association({'paths': ['data/references']}, files, ['Eye.csv'])
        self.assertEqual(matched, [files[1]])

    def test_archive_limits_do_not_become_a_pass_and_no_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'example.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('../outside.csv', 'field\nvalue\n')
            result = workspace.archive_profile(archive, {
                'max_decompressed_bytes_per_file': 1, 'max_member_bytes_to_parse': 100,
                'max_archive_members': 10})
            self.assertEqual(result['state'], 'not_fully_checked')
            self.assertEqual(result['members_not_read_budget'], 1)
            self.assertIn('../outside.csv', result['unsafe_or_link_members'])
            self.assertEqual(list(Path(directory).iterdir()), [archive])

    def test_missing_prepared_file_cannot_shrink_an_allowed_scope(self):
        class Licenses:
            def check(self, *args):
                return {'status': 'allowed'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'inputs').mkdir()
            (root / 'inputs/present.csv').write_text('field\nvalue\n')
            prepared = {'inputs/present.csv': {}, 'inputs/missing.csv': {}}
            artifacts = {'inputs/present.csv': {
                'integrity': 'verified', 'actual_sha256': 'a',
                'structure': {'state': 'passed_structure'}}}
            with patch.object(workspace, 'ROOT', root):
                result = workspace.scope_check({'path': 'inputs'}, artifacts, Licenses(), prepared)
            self.assertFalse(result['prepared_manifest_bound'])
            self.assertEqual(result['status'], 'blocked_or_incomplete')

    def test_all_curated_ids_exist_and_source_scopes_never_include_answers(self):
        policy = json.loads((ROOT / 'configs/dataset_validation.json').read_text())
        registry = json.loads((ROOT / 'configs/dataset_study_usage.json').read_text())
        ids = {d['id'] for d in registry['datasets']}
        for group in policy['groups'].values():
            self.assertTrue(set(group) <= ids)
        for scope in policy['input_scopes']:
            self.assertIn(scope['dataset'], ids)
            paths = scope.get('files', [scope.get('path')])
            if scope['id'] == 'pharmaverse_generated_response':
                self.assertEqual(len(paths), 3)
                self.assertIn('cannot also be gold', scope['task'])
            else:
                self.assertTrue(all('/inputs/' in p or '/schema_inputs/' in p for p in paths))

    def test_html_response_never_passes_as_a_rectangular_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.csv'
            path.write_text('<!doctype html><html>Login</html>')
            result = workspace.structure(path, {})
            self.assertEqual(result['state'], 'failed')

    def test_semicolon_csv_is_read_without_editing_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.csv'
            path.write_text('name;description\na;"contains,comma"\n')
            before = path.read_bytes()
            result = workspace.structure(path, {})
            self.assertEqual(result['state'], 'passed_structure')
            self.assertEqual(result['columns'], 2)
            self.assertEqual(path.read_bytes(), before)

    def test_empty_meta_count_response_is_not_a_json_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'count.json'
            path.write_bytes(b'')
            entry = {'path': 'count.json', 'bytes': 0,
                     'sha256': hashlib.sha256(b'').hexdigest(),
                     'response_format': 'empty_META_body_with_count_headers',
                     'response_headers': {'total-count': '42'}}
            with patch.object(workspace, 'ROOT', root):
                result = workspace.check_artifact(entry, {})
            self.assertEqual(result['structure']['state'], 'not_dataset_response')
            self.assertEqual(result['structure']['provider_total_count'], 42)

    def test_a_licensed_reference_cannot_become_source_input_by_path_only(self):
        class Licenses:
            def check(self, *args):
                return {'status': 'allowed'}
        prepared = {'references/answers.csv': {'prepared_role': 'reference'}}
        artifacts = {'references/answers.csv': {'integrity': 'verified',
                     'actual_sha256': 'a', 'structure': {'state': 'passed_structure'}}}
        scope = {'id': 'some_source', 'files': ['references/answers.csv']}
        result = workspace.scope_check(scope, artifacts, Licenses(), prepared)
        self.assertFalse(result['source_role_verified'])
        self.assertEqual(result['status'], 'blocked_or_incomplete')


if __name__ == '__main__':
    unittest.main()
