"""Download status must not promote documentation, obsolete routes or corruption."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('dataset_download_audit',
    Path(__file__).parents[1] / 'Scripts/audit_dataset_downloads.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class DatasetDownloadAuditTests(unittest.TestCase):
    def test_a_locked_and_verified_job_counts_as_downloaded_without_a_row_in_the_acquisition_report(self):
        job = {'id': 'a', 'acquisition': 'direct', 'url': 'https://x.example/a.csv', 'path': 'datasets/x/a.csv'}
        held = {'id': 'a', 'state': 'verified', 'url': 'https://x.example/a.csv', 'path': 'data/acquisitions/datasets/x/a.csv'}
        self.assertEqual(audit.lock_outcome(job, held)['state'], 'downloaded_verified')
        self.assertEqual(audit.lock_outcome(job, None)['state'], 'not_yet_downloaded')
        self.assertEqual(audit.lock_outcome(job, {**held, 'state': 'integrity_failure'})['state'], 'not_yet_downloaded', 'a file failing its hash is not counted as held')
        self.assertEqual(audit.lock_outcome(job, {**held, 'url': 'https://x.example/other.csv'})['state'], 'not_yet_downloaded')
        self.assertEqual(audit.lock_outcome({**job, 'acquisition': 'manual', 'reason': 'login'}, None), {'id': 'a', 'state': 'manual', 'reason': 'login'})

    def test_fresh_hash_detects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.bin'
            path.write_bytes(b'changed')
            result = audit.verify({'absolute_path': str(path), 'path': 'data.bin',
                'bytes': 7, 'sha256': hashlib.sha256(b'correct').hexdigest()})
            self.assertEqual(result['state'], 'mismatch')

    def test_html_response_never_becomes_a_tabular_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.csv'
            body = b'<!doctype html><html>Login</html>'
            path.write_bytes(body)
            result = audit.verify({'absolute_path': str(path), 'path': 'data.csv',
                'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
            self.assertEqual(result['state'], 'verified')
            self.assertEqual(result['content_state'], 'unexpected_html_in_dataset')

    def test_cms_same_archive_basename_can_contain_distinct_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);records=[]
            for route,sample in [('stats',1),('downloads',20)]:
                path=root/'data/datasets/cms_de_synpuf'/route/'sample20.zip'
                path.parent.mkdir(parents=True)
                with zipfile.ZipFile(path,'w') as archive:
                    archive.writestr(f'DE1_0_2010_Beneficiary_Summary_File_Sample_{sample}.csv','field\n')
                records.append({'path':str(path.relative_to(root)),'state':'verified'})
            with patch.object(audit,'ROOT',root):result=audit.cms_members(records)
            self.assertEqual(result['held_data_members'],2)
            self.assertNotIn('de1_0_2010_beneficiary_summary_file_sample_1.csv',result['missing_data_members'])
            self.assertNotIn('de1_0_2010_beneficiary_summary_file_sample_20.csv',result['missing_data_members'])

    def test_selected_sample_scope_does_not_demand_nineteen_more_populations(self):
        result = audit.cms_members([], samples=[1])
        self.assertEqual(result['expected_data_members'], 8)
        self.assertTrue(all('_sample_1' in name for name in result['missing_data_members']))

    def test_provider_index_is_not_a_dataset(self):
        catalog = [{'id': 'D34', 'name': 'Clinical study', 'url': 'https://provider.example'}]
        jobs = [{'id': 'index', 'catalog_ids': ['D34'], 'kind': 'provider_index',
                 'path': 'index.html', 'acquisition': 'direct'}]
        rows = audit.reconcile(catalog, jobs, {'index': {'state': 'downloaded_verified'}},
                               [{'id': 'index', 'path': 'index.html', 'state': 'verified'}])
        self.assertEqual(rows[0]['state'], 'provider_docs_only_data_missing')

    def test_additional_native_release_is_included_in_catalog_locations(self):
        catalog = [{'id': 'D04', 'name': 'MIMIC demos', 'url': 'https://provider.example',
                    'local_evidence': 'downloaded_data/omop',
                    'additional_local_evidence': ['downloaded_data/native']}]
        prefix = str(audit.COLLECTION.relative_to(audit.ROOT))
        paths = [prefix + '/downloaded_data/omop/person.csv',
                 prefix + '/downloaded_data/native/hosp/labevents.csv.gz']
        rows = audit.reconcile(catalog, [], {}, [{'path': p, 'state': 'verified'} for p in paths])
        self.assertEqual(set(rows[0]['verified_artifacts']), set(paths))

    def test_local_links_keep_participant_files_separate_from_provider_docs(self):
        prefix = str(audit.BASE.relative_to(audit.ROOT))
        rendered = audit.local_links([prefix + '/datasets/geo/GSE1/matrix.csv',
                                      prefix + '/provider/geo/GSE1/index.html'])
        self.assertIn('/datasets/geo/GSE1/matrix.csv)', rendered)
        self.assertIn('/provider/geo/GSE1/index.html)', rendered)
        self.assertEqual(rendered.count('<br>'), 1)

    def test_superseded_archive_does_not_make_complete_pinned_files_missing(self):
        catalog = [{'id': 'D34', 'name': 'Clinical study', 'url': 'https://provider.example'}]
        jobs = [{'id': 'old', 'catalog_ids': ['D34'], 'kind': 'dataset', 'path': 'old.zip',
                 'acquisition': 'superseded_by_pinned_files'},
                {'id': 'pinned', 'catalog_ids': ['D34'], 'kind': 'dataset_component',
                 'path': 'data.txt', 'acquisition': 'direct'}]
        rows = audit.reconcile(catalog, jobs,
            {'old': {'state': 'superseded_by_pinned_files'}, 'pinned': {'state': 'downloaded_verified'}},
            [{'id': 'pinned', 'path': 'data.txt', 'state': 'verified'}])
        self.assertEqual(rows[0]['missing_public_artifacts'], [])
        self.assertEqual(rows[0]['state'], 'verified_public_data_or_fixture')


if __name__ == '__main__':
    unittest.main()
