"""Full-release resolution: provider detection, manifest parsing, gates and registration with fixture responses; no network."""
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

CODE_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('resolve_full_release', CODE_ROOT / 'Scripts/resolve_full_release.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1) if not isinstance(value, str) else value)


class FakeWeb:
    """Canned HTTPS responses keyed by URL: (status, body) or (status, body, final_url, headers)."""

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def fetch(self, url, *, limit=None, method='GET'):
        self.calls.append((method, url))
        status, body, *rest = self.responses.get(url, (404, None))
        final = rest[0] if rest else url
        headers = rest[1] if len(rest) > 1 else {'Content-Type': 'application/json'}
        if method == 'HEAD':
            return status, final, {'Content-Length': str(len(body))} if body is not None else {}, None
        return status, final, headers, body


class DetectTests(unittest.TestCase):
    def test_provider_detection_from_source_urls(self):
        self.assertEqual(module.detect(['https://physionet.org/content/eicu-crd-demo/2.0.1/'])['provider'], 'physionet')
        self.assertEqual(module.detect(['https://zenodo.org/records/5084605'])['record'], '5084605')
        self.assertEqual(module.detect(['https://github.com/OHDSI/EunomiaDatasets'])['repo'], 'EunomiaDatasets')
        self.assertEqual(module.detect(['https://huggingface.co/datasets/inria-soda/carte-benchmark'])['name'], 'carte-benchmark')
        self.assertEqual(module.detect(['https://www.openml.org/s/271'])['kind'], 'study')
        self.assertEqual(module.detect(['https://www.openml.org/d/31'])['kind'], 'data')
        self.assertIsNone(module.detect(['https://www.kaggle.com/datasets/x/y', 'https://example.org/data']))
        targets, unsupported = module.detect_all(['https://zenodo.org/records/5', 'https://doi.org/10.5281/zenodo.5', 'https://doi.org/10.1038/s41598-025-06447-2', 'https://arxiv.org/abs/2407.11852',
                                                 'https://physionet.org/content/demo/1.0/', 'https://physionet.org/content/demo/view-license/1.0/', 'https://www.kaggle.com/datasets/x/y'])
        self.assertEqual([t['provider'] for t in targets], ['zenodo', 'physionet'], 'a Zenodo DOI is the same record; papers and licence pages are references')
        self.assertEqual(unsupported, ['https://www.kaggle.com/datasets/x/y'])


class ManifestTests(unittest.TestCase):
    def test_physionet_checksums_sizes_and_access_states(self):
        sums = b'a' * 64 + b'  LICENSE.txt\n' + b'b' * 64 + b'  hosp/lab.csv.gz\n'
        web = FakeWeb({'https://physionet.org/files/demo/1.0/SHA256SUMS.txt': (200, sums, 'https://physionet.org/files/demo/1.0/SHA256SUMS.txt', {'Content-Type': 'text/plain'})})
        with patch.object(module, 'fetch', web.fetch), patch.object(module, 'head_sizes', lambda urls, workers=3: {u: 11 for u in urls}):
            evidence = []
            resolved = module.resolve_physionet({'project': 'demo', 'version': '1.0'}, evidence)
        self.assertEqual([(f['path'], f['provider_sha256'], f['bytes'], f['format']) for f in resolved['files']], [('LICENSE.txt', 'a' * 64, 11, 'txt'), ('hosp/lab.csv.gz', 'b' * 64, 11, 'gzip')])
        self.assertEqual(evidence[0][0], 'physionet_demo_1.0_SHA256SUMS.txt')
        login = FakeWeb({'https://physionet.org/files/cred/1.0/SHA256SUMS.txt': (200, b'<html>login</html>', 'https://physionet.org/login/?next=/files/cred/1.0/', {'Content-Type': 'text/html'})})
        with patch.object(module, 'fetch', login.fetch):
            self.assertEqual(module.resolve_physionet({'project': 'cred', 'version': '1.0'}, [])['blocked'], 'blocked_access_credentialed')
        index = b'<a href="../">../</a>\n<a href="set-a/">set-a/</a>  17-Apr-2019 13:41  -\n<a href="Outcomes-a.txt">Outcomes-a.txt</a>  17-Apr-2019 13:48  79219\n'
        sub = b'<a href="../">../</a>\n<a href="132539.txt">132539.txt</a>  17-Apr-2019 13:41  7000\n'
        open_project = FakeWeb({'https://physionet.org/files/open/1.0/SHA256SUMS.txt': (404, None), 'https://physionet.org/files/open/1.0/': (200, index), 'https://physionet.org/files/open/1.0/set-a/': (200, sub)})
        with patch.object(module, 'fetch', open_project.fetch):
            resolved = module.resolve_physionet({'project': 'open', 'version': '1.0'}, [])
        self.assertEqual([(f['path'], f['bytes'], 'provider_sha256' in f) for f in resolved['files']], [('Outcomes-a.txt', 79219, False), ('set-a/132539.txt', 7000, False)])
        self.assertIn('no checksums', resolved['manifest'])

    def test_zenodo_github_and_openml_manifests(self):
        record = {'metadata': {'access_right': 'open', 'license': {'id': 'cc-by-4.0'}, 'version': '1.0'}, 'files': [{'key': 'data.zip', 'size': 10, 'checksum': 'md5:' + 'c' * 32}]}
        tree = {'sha': 'f' * 40, 'truncated': False, 'tree': [{'type': 'blob', 'path': 'LICENSE', 'size': 5, 'sha': '1' * 40}, {'type': 'tree', 'path': 'src', 'sha': '2' * 40}, {'type': 'blob', 'path': 'src/a.py', 'size': 7, 'sha': '3' * 40}]}
        release = {'tag_name': 'v1.0', 'assets': [{'name': 'pkg.whl', 'size': 9, 'digest': 'sha256:' + 'd' * 64, 'browser_download_url': 'https://github.com/o/r/releases/download/v1.0/pkg.whl'}]}
        web = FakeWeb({'https://zenodo.org/api/records/1': (200, json.dumps(record).encode()),
                       'https://api.github.com/repos/o/r': (200, json.dumps({'full_name': 'o/r', 'default_branch': 'main', 'license': {'spdx_id': 'MIT'}}).encode()),
                       'https://api.github.com/repos/o/r/releases/latest': (200, json.dumps(release).encode()),
                       'https://api.github.com/repos/o/r/git/ref/tags/v1.0': (200, json.dumps({'object': {'sha': 'f' * 40, 'type': 'commit'}}).encode()),
                       'https://api.github.com/repos/o/r/git/trees/' + 'f' * 40 + '?recursive=1': (200, json.dumps(tree).encode()),
                       'https://www.openml.org/api/v1/json/study/9': (200, json.dumps({'study': {'data': {'data_id': [31]}}}).encode()),
                       'https://www.openml.org/api/v1/json/data/31': (200, json.dumps({'data_set_description': {'id': '31', 'name': 'credit-g', 'version': '1', 'licence': 'Public', 'format': 'ARFF', 'url': 'https://openml.org/data/v1/download/31/credit-g.arff', 'md5_checksum': 'e' * 32}}).encode()),
                       'https://openml.org/data/v1/download/31/credit-g.arff': (200, b'x' * 42)})
        with patch.object(module, 'fetch', web.fetch):
            zenodo = module.resolve_zenodo({'record': '1'}, [])
            github = module.resolve_github({'owner': 'o', 'repo': 'r'}, [])
            openml = module.resolve_openml({'kind': 'study', 'id': '9'}, [])
        self.assertEqual(zenodo['files'][0]['provider_md5'], 'c' * 32)
        self.assertEqual(zenodo['licence_label'], 'cc-by-4.0')
        self.assertEqual([(f['path'], f.get('provider_sha256'), f.get('git_blob_sha1')) for f in github['files']],
                         [('release_assets/pkg.whl', 'd' * 64, None), ('source/LICENSE', None, '1' * 40), ('source/src/a.py', None, '3' * 40)])
        self.assertIn('release v1.0', github['release'])
        self.assertEqual((openml['files'][0]['provider_md5'], openml['files'][0]['bytes'], openml['licence_label']), ('e' * 32, 42, {'Public': 1}))
        limited = FakeWeb({'https://api.github.com/repos/o/r': (403, None, 'https://api.github.com/repos/o/r', {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1794182400'})})
        with patch.object(module, 'fetch', limited.fetch):
            self.assertTrue(module.resolve_github({'owner': 'o', 'repo': 'r'}, [])['blocked'].startswith('github_api_rate_limited_retry_after_2026'))
        closed = FakeWeb({'https://zenodo.org/api/records/2': (200, json.dumps({'metadata': {'access_right': 'restricted'}, 'files': []}).encode())})
        with patch.object(module, 'fetch', closed.fetch):
            self.assertEqual(module.resolve_zenodo({'record': '2'}, [])['blocked'], 'blocked_access_restricted')


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        today = date.today()
        write(self.root / 'study.json', {'schema_version': 1, 'id': 'demo-study'})
        self.study = {'id': 'demo-study', 'name': 'Demo', 'question': 'q', 'intended_use': 'u', 'population': '', 'targets': '', 'inclusion': '', 'exclusion': ''}
        write(self.root / 'configs/research_studies.json', {'schema_version': 1, 'default_study': 'demo-study', 'studies': [{**self.study, 'dataset_ids': [], 'dataset_annotations': {}, 'study_annotations': {}}]})
        self.dataset = {'id': 'ZEN', 'name': 'Zenodo set', 'source_urls': ['https://zenodo.org/records/1'], 'access_route': 'public_no_registration', 'category': 'mapping_references', 'priority': 'P1',
                        'obtained_artifacts': [], 'missing_components': []}
        selection = self.root / 'data/interim/dataset_selection/20260101T000000000000Z/selection.json'
        write(selection, {'datasets': [self.dataset]})
        self.catalogue_sha = hashlib.sha256(selection.read_bytes()).hexdigest()
        write(self.root / 'data/interim/dataset_selection/latest.json', {'selection': str(selection.relative_to(self.root)), 'selection_sha256': self.catalogue_sha})
        write(self.root / 'configs/research_resources.json', {'schema_version': 1, 'scope': 'fixture', 'resources': []})
        write(self.root / 'configs/research_resources.lock.json', {'schema_version': 1, 'files': []})
        write(self.root / 'configs/resource_scope.json', {'decisions': []})
        write(self.root / 'configs/dataset_download_reviews.json', {'reviews': [], 'approved_artifacts': []})
        write(self.root / 'configs/data_license_assets.json', {'schema_version': 1, 'as_of': today.isoformat(), 'files': []})
        notice = self.root / 'notice.txt'
        notice.write_text('CC BY 4.0')
        self.licence = {'id': 'zen_cc_by', 'name': 'Zenodo set', 'license': 'CC-BY-4.0', 'scope': 'data', 'reviewed_on': today.isoformat(), 'review_due': (today + timedelta(days=30)).isoformat(),
                        'study_id': 'demo-study', 'reason': 'ok', 'conditions': [], 'sources': [], 'catalog_ids': ['ZEN'],
                        'operations': {'local_inference': 'allowed', 'local_embedding': 'allowed'}, 'ai_permissions': {},
                        'evidence': [{'snapshot': 'notice.txt', 'sha256': hashlib.sha256(notice.read_bytes()).hexdigest()}]}
        write(self.root / 'configs/data_licenses.json', {'schema_version': 1, 'default': 'lenient', 'resources': [self.licence]})
        self.spec = {'kind': 'datahunter_download_instructions', 'schema_version': 1, 'catalogue_sha256': self.catalogue_sha, 'dataset_ids': ['ZEN'], 'research_study': self.study,
                     'download_scope': 'full', 'license_mode': 'lenient', 'max_bytes_per_dataset': 20_000_000_000, 'max_bytes_per_artifact': 20_000_000_000, 'max_total_bytes': 20_000_000_000,
                     'technical_risk_acknowledgements': []}
        self.record = {'metadata': {'access_right': 'open', 'license': {'id': 'cc-by-4.0'}}, 'files': [{'key': 'data.zip', 'size': 1000, 'checksum': 'md5:' + 'c' * 32}, {'key': 'README.md', 'size': 20, 'checksum': 'md5:' + 'a' * 32}]}
        self.web = FakeWeb({'https://zenodo.org/api/records/1': (200, json.dumps(self.record).encode())})
        self.instructions = self.root / 'instructions.json'
        write(self.instructions, self.spec)

    def registry(self, mode='lenient'):
        return module.LicenseRegistry(self.root / 'configs/data_licenses.json', self.root / 'configs/data_license_assets.json', root=self.root, mode=mode, study_id='demo-study')

    def resolution(self):
        with patch.object(module, 'fetch', self.web.fetch):
            return module.resolve_zenodo({'record': '1'}, [])


class GateTests(WorkspaceFixture):
    def test_gates_size_identity_and_licence_binding(self):
        resolved = self.resolution()
        plan = module.plan_dataset(self.dataset, resolved, self.spec, self.registry(), set(), set(), set())
        self.assertEqual((plan['outcome'], len(plan['jobs']), plan['bindings']), ('ready', 2, ['zen_cc_by']))
        self.assertEqual(plan['jobs'][0]['download_license_resources'], ['zen_cc_by'])
        self.assertTrue(all(j['max_bytes'] >= j['expected_bytes'] for j in plan['jobs']))
        zip_job = next(j for j in plan['jobs'] if j['path'].endswith('.zip'))
        self.assertEqual(zip_job['max_uncompressed_bytes'], self.spec['max_bytes_per_dataset'], 'archive inspection is bounded by the saved dataset ceiling, not a fixed 2 GiB')
        small = {**self.spec, 'max_bytes_per_dataset': 500, 'max_bytes_per_artifact': 500}
        self.assertEqual(module.plan_dataset(self.dataset, resolved, small, self.registry(), set(), set(), set())['outcome'], 'blocked_dataset_size_limit')
        self.assertEqual(module.plan_dataset(self.dataset, resolved, self.spec, self.registry('restrictive'), set(), set(), set())['outcome'], 'blocked_license_binding_missing',
                         'an AI-silent licence binds nothing in restrictive mode')
        unsigned = {**resolved, 'files': [{**f, 'provider_md5': None} for f in resolved['files']]}
        for f in unsigned['files']:
            f.pop('provider_md5')
        plan = module.plan_dataset(self.dataset, unsigned, self.spec, self.registry(), set(), set(), set())
        self.assertEqual((plan['outcome'], plan['unaccepted']), ('needs_technical_risk_acknowledgement', ['missing_provider_checksum']))
        self.assertEqual(module.plan_dataset(self.dataset, unsigned, self.spec, self.registry(), set(), set(), {'missing_provider_checksum'})['outcome'], 'ready')
        mixed = {**resolved, 'files': [resolved['files'][0], {k: v for k, v in resolved['files'][1].items() if k != 'provider_md5'}]}
        plan = module.plan_dataset(self.dataset, mixed, self.spec, self.registry(), set(), set(), set())
        self.assertEqual((plan['outcome'], len(plan['jobs']), plan['unacknowledged_files'], plan['unaccepted']), ('ready_partial', 1, 1, ['missing_provider_checksum']), 'identity-backed files proceed; the rest waits for the tick')
        unsized = {**resolved, 'files': [resolved['files'][0], {**resolved['files'][1], 'bytes': None}, {**resolved['files'][1], 'path': 'README2.md', 'url': 'https://zenodo.org/u3', 'bytes': None}]}
        plan = module.plan_dataset(self.dataset, unsized, self.spec, self.registry(), set(), set(), set())
        self.assertEqual((plan['outcome'], plan['unknown_size_files'], plan['unaccepted'], plan['package_budget']), ('ready_partial', 2, ['unknown_advertised_size'], plan['jobs'][0]['max_bytes']), 'unknown sizes are reported, not charged at the artifact cap')
        plan = module.plan_dataset(self.dataset, unsized, self.spec, self.registry(), set(), set(), {'unknown_advertised_size'})
        self.assertEqual((plan['outcome'], len(plan['jobs']), plan['jobs'][1]['max_bytes']), ('ready', 3, self.spec['max_bytes_per_artifact']), 'acknowledged unknown sizes transfer under the hard per-artifact cap')
        held = {('url', 'https://zenodo.org/api/records/1/files/data.zip/content')}
        plan = module.plan_dataset(self.dataset, resolved, self.spec, self.registry(), set(), held, set())
        self.assertEqual((plan['held'], len(plan['jobs'])), (1, 1), 'a file already held under any folder name is not re-registered')
        self.assertEqual(module.plan_dataset(self.dataset, {'blocked': 'blocked_access_restricted'}, self.spec, self.registry(), set(), set(), set())['outcome'], 'blocked_access_restricted')

    def test_code_releases_travel_as_bounded_references_without_a_data_binding(self):
        code = {**self.dataset, 'category': 'standards_and_methods'}
        resolved = {'release': 'GitHub o/r release v1', 'manifest': 'tree', 'folder': 'o_r_v1', 'files': [{'path': 'source/LICENSE', 'url': 'https://raw.githubusercontent.com/o/r/f/LICENSE', 'bytes': 5, 'format': 'bin', 'git_blob_sha1': '1' * 40}]}
        write(self.root / 'configs/data_licenses.json', {'schema_version': 1, 'default': 'lenient', 'resources': [{**self.licence, 'scope': 'software_only'}]})
        plan = module.plan_dataset(code, resolved, self.spec, self.registry(), set(), set(), set())
        self.assertEqual((plan['outcome'], plan['jobs'][0]['kind'], plan['bindings']), ('ready', 'repository_source', []))

    def test_coverage_limits_bind_only_listed_urls_and_the_release_is_labelled_partial(self):
        resolved = self.resolution()
        write(self.root / 'configs/data_licenses.json', {'schema_version': 1, 'default': 'lenient', 'resources': [{**self.licence, 'covers_urls': ['https://zenodo.org/api/records/1/files/data.zip/content']}]})
        plan = module.plan_dataset(self.dataset, resolved, self.spec, self.registry(), set(), set(), set())
        self.assertEqual((plan['outcome'], [j['path'] for j in plan['jobs']], plan['unbound_files'], plan['complete_release_registrable']), ('ready_partial', ['datasets/zenodo_1/data.zip'], 1, False))
        self.assertEqual(plan['unbound_sample'], ['datasets/zenodo_1/README.md'])

    def test_unresolved_rights_option_registers_open_files_flagged_and_never_credentialed_ones(self):
        resolved = self.resolution()
        write(self.root / 'configs/data_licenses.json', {'schema_version': 1, 'default': 'lenient', 'resources': []})  # no licence record at all
        plan = module.plan_dataset(self.dataset, resolved, self.spec, self.registry(), set(), set(), set())
        self.assertEqual(plan['outcome'], 'blocked_license_binding_missing')
        plan = module.plan_dataset(self.dataset, resolved, {**self.spec, 'acquire_unresolved_rights': True}, self.registry(), set(), set(), set())
        self.assertEqual((plan['outcome'], len(plan['jobs']), plan['rights_unresolved_files'], plan['bindings']), ('ready', 2, 2, []))
        self.assertTrue(all(j['rights_unresolved'] and j['download_license_resources'] == [] for j in plan['jobs']))
        credentialed = {**self.dataset, 'access_route': 'registration_credentialed_access'}
        plan = module.plan_dataset(credentialed, resolved, {**self.spec, 'acquire_unresolved_rights': True}, self.registry(), set(), set(), set())
        self.assertEqual(plan['outcome'], 'blocked_license_binding_missing', 'the option never applies to a registration route')
        module.register(self.root, self.dataset, resolved, module.plan_dataset(self.dataset, resolved, {**self.spec, 'acquire_unresolved_rights': True}, self.registry(), set(), set(), set()), [], [], 'run')
        approvals = json.loads((self.root / 'configs/dataset_download_reviews.json').read_text())['approved_artifacts']
        self.assertTrue(all(a.get('rights_unresolved_download') is True and 'download_license_resources' not in a for a in approvals))
        self.assertEqual(json.loads((self.root / 'configs/data_license_assets.json').read_text())['files'], [], 'no licence asset is invented for rights-unresolved files')

    def test_unsupported_source_components_keep_a_release_partial_and_ids_never_collide(self):
        with patch.object(module, 'fetch', self.web.fetch):
            resolution = module.resolve_all(*module.detect_all(['https://zenodo.org/records/1', 'https://www.kaggle.com/datasets/x/y']), [])
        self.assertEqual(resolution['unsupported_urls'], ['https://www.kaggle.com/datasets/x/y'])
        self.assertEqual(len(resolution['components']), 1)
        plan = module.plan_dataset(self.dataset, resolution, self.spec, self.registry(), set(), set(), set())
        self.assertEqual((plan['outcome'], plan['complete_release_registrable']), ('ready_partial', False))
        twins = {**resolution, 'files': [{**resolution['files'][0], 'path': 'a-b.csv', 'url': 'https://zenodo.org/u1'}, {**resolution['files'][0], 'path': 'a_b.csv', 'url': 'https://zenodo.org/u2'}]}
        plan = module.plan_dataset(self.dataset, twins, self.spec, self.registry(), set(), set(), set())
        self.assertEqual(len({j['id'] for j in plan['jobs']}), 2, 'paths that slug alike get distinct, deterministic ids')


class RunTests(WorkspaceFixture):
    def test_dry_run_resolves_and_register_writes_jobs_approvals_evidence_and_log(self):
        with patch.object(module, 'fetch', self.web.fetch):
            report = module.run(self.instructions, root=self.root)
        self.assertEqual(report['counts'], {'ready': 1})
        self.assertEqual(report['outcomes'][0]['release_files'], 2)
        self.assertEqual(json.loads((self.root / 'configs/research_resources.json').read_text())['resources'], [], 'a dry run registers nothing')
        self.assertFalse((self.root / 'actions.jsonl').exists())
        with patch.object(module, 'fetch', self.web.fetch):
            report = module.run(self.instructions, root=self.root, register_jobs=True)
        manifest = json.loads((self.root / 'configs/research_resources.json').read_text())['resources']
        self.assertEqual([(j['path'], j['provider_md5'], j['expected_bytes']) for j in manifest], [('datasets/zenodo_1/data.zip', 'c' * 32, 1000), ('datasets/zenodo_1/README.md', 'a' * 32, 20)])
        reviews = json.loads((self.root / 'configs/dataset_download_reviews.json').read_text())
        self.assertEqual(reviews['reviews'][0]['full_release']['files'], 2)
        self.assertEqual([a['download_license_resources'] for a in reviews['approved_artifacts']], [['zen_cc_by'], ['zen_cc_by']])
        evidence = reviews['approved_artifacts'][0]['metadata_evidence']
        self.assertTrue(any(e['path'].endswith('zenodo_1_record.json') for e in evidence) and any(e['path'] == 'notice.txt' for e in evidence))
        for item in evidence:
            self.assertEqual(hashlib.sha256((self.root / item['path']).read_bytes()).hexdigest(), item['sha256'])
        scope = json.loads((self.root / 'configs/resource_scope.json').read_text())['decisions']
        self.assertEqual([d['retain'] for d in scope], [True, True])
        log = [json.loads(l) for l in (self.root / 'actions.jsonl').read_text().splitlines()]
        self.assertEqual(log[-1]['action'], 'download_run')
        self.assertIn('ZEN: ready (2 jobs, 1,020 bytes)', log[-1]['detail'])
        with patch.object(module, 'fetch', self.web.fetch):
            again = module.run(self.instructions, root=self.root, register_jobs=True)
        self.assertEqual((again['counts'], again['outcomes'][0]['new_jobs']), ({'ready': 1}, 2), 'registered but unheld jobs (failed or interrupted transfers) are resumed under their own ids')
        manifest = json.loads((self.root / 'configs/research_resources.json').read_text())['resources']
        self.assertEqual(len(manifest), 2, 'a resumed job replaces its earlier definition; nothing is duplicated')
        self.assertEqual(len(json.loads((self.root / 'configs/dataset_download_reviews.json').read_text())['approved_artifacts']), 2)
        taken = module.plan_dataset(self.dataset, self.resolution(), self.spec, self.registry(), {'datasets/zenodo_1/data.zip': 'another_job'}, set(), set())
        self.assertEqual(taken['outcome'], 'path_already_registered_under_another_job', 'a path registered under a different job still blocks')

    def test_access_routes_are_applied_before_any_provider_request(self):
        selection = self.root / 'data/interim/dataset_selection/20260101T000000000000Z/selection.json'
        write(selection, {'datasets': [{**self.dataset, 'access_route': 'registration_credentialed_access'}]})
        write(self.root / 'data/interim/dataset_selection/latest.json', {'selection': str(selection.relative_to(self.root)), 'selection_sha256': hashlib.sha256(selection.read_bytes()).hexdigest()})
        write(self.instructions, {**self.spec, 'catalogue_sha256': hashlib.sha256(selection.read_bytes()).hexdigest()})
        with patch.object(module, 'fetch', side_effect=AssertionError('no provider request for a registration route')):
            report = module.run(self.instructions, root=self.root)
        self.assertEqual(report['counts'], {'blocked_access_registration_credentialed_access': 1})

    def test_scope_hash_and_study_are_enforced(self):
        write(self.instructions, {**self.spec, 'download_scope': 'minimal'})
        with self.assertRaisesRegex(ValueError, 'download_scope_full'):
            module.run(self.instructions, root=self.root)
        write(self.instructions, {**self.spec, 'catalogue_sha256': 'a' * 64})
        with self.assertRaisesRegex(ValueError, 'catalogue_changed'):
            module.run(self.instructions, root=self.root)
        write(self.instructions, {**self.spec, 'research_study': {**self.study, 'question': 'other'}})
        with self.assertRaisesRegex(ValueError, 'register_current_research_study'):
            module.run(self.instructions, root=self.root)

    def test_execute_locks_identity_backed_files_and_quarantines_acknowledged_risks(self):
        record = {'metadata': {'access_right': 'open'}, 'files': [{'key': 'data.zip', 'size': 1000, 'checksum': 'md5:' + 'c' * 32}, {'key': 'notes.txt', 'size': 20}]}
        self.web = FakeWeb({'https://zenodo.org/api/records/1': (200, json.dumps(record).encode())})
        write(self.instructions, {**self.spec, 'technical_risk_acknowledgements': ['missing_provider_checksum']})

        def fake_acquire(item, locked, base, execute, journal, license_context, **options):
            path = base / item['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * (item.get('expected_bytes') or 1))
            return {'id': item['id'], 'path': item['path'], 'url': item['url'], 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'hash_authority': 'fixture', 'acquired_at': '2026-01-01T00:00:00+00:00', 'state': 'downloaded_verified', 'inspection': None}
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'acquire', fake_acquire):
            report = module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True)
        self.assertEqual(report['execution'], {'acquired': 1, 'failed': 0, 'quarantined': 1, 'lock_files': 1})
        lock = json.loads((self.root / 'configs/research_resources.lock.json').read_text())['files']
        self.assertEqual([f['path'] for f in lock], ['datasets/zenodo_1/data.zip'])
        staged = list((self.root / 'data/quarantine/download_agent').rglob('notes.txt'))
        self.assertEqual(len(staged), 1, 'the checksum-less file is staged in quarantine, not locked')
        run_report = json.loads(next((self.root / 'runs/downloads').rglob('report.json')).read_text())
        self.assertEqual(run_report['unique_artifacts'][0]['status'], 'downloaded_quarantined')
        self.assertEqual(run_report['unique_artifacts'][0]['technical_risks'], ['missing_provider_checksum'])

    def test_concurrent_transfers_settle_every_file_into_the_lock_and_journal(self):
        files = [{'key': f'part{i}.csv', 'size': 10 + i, 'checksum': 'md5:' + f'{i:032x}'} for i in range(9)]
        self.web = FakeWeb({'https://zenodo.org/api/records/1': (200, json.dumps({'metadata': {'access_right': 'open'}, 'files': files}).encode())})
        write(self.instructions, self.spec)
        threads = set()

        def fake_acquire(item, locked, base, execute, journal, license_context, **options):
            import threading, time
            threads.add(threading.get_ident()); time.sleep(0.01)
            journal('transfer_started', {'id': item['id']})
            path = base / item['path']; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'x' * item['expected_bytes'])
            return {'id': item['id'], 'path': item['path'], 'url': item['url'], 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'hash_authority': 'fixture', 'acquired_at': '2026-01-01T00:00:00+00:00', 'state': 'downloaded_verified', 'inspection': None}
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'acquire', fake_acquire):
            report = module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True, workers=3)
        self.assertEqual(report['execution'], {'acquired': 9, 'failed': 0, 'quarantined': 0, 'lock_files': 9})
        self.assertGreater(len(threads), 1, 'transfers ran on more than one worker')
        lock = json.loads((self.root / 'configs/research_resources.lock.json').read_text())['files']
        self.assertEqual(sorted(f['path'] for f in lock), sorted(f"datasets/zenodo_1/{f['key']}" for f in files))
        rows = [json.loads(l) for l in (self.root / report['run'] / 'operations.jsonl').read_text().splitlines()]
        self.assertEqual((sum(r['kind'] == 'transfer_started' for r in rows), sum(r['kind'] == 'acquired' for r in rows)), (9, 9), 'every journal line is intact')

    def test_a_complete_resolution_clears_a_stale_partial_review_record(self):
        record = {'metadata': {'access_right': 'open'}, 'files': [{'key': 'data.zip', 'size': 1000, 'checksum': 'md5:' + 'c' * 32}]}
        self.web = FakeWeb({'https://zenodo.org/api/records/1': (200, json.dumps(record).encode())})
        write(self.instructions, self.spec)
        write(self.root / 'configs/dataset_study_usage.json', {'datasets': [{'id': self.dataset['id'], 'name': 'Zenodo set', 'source_urls': ['https://zenodo.org/records/1'], 'paths': [], 'completeness': 'not_obtained', 'missing_components': [], 'next_action': ''}]})

        def fake_acquire(item, locked, base, execute, journal, license_context, **options):
            path = base / item['path']; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'x' * item['expected_bytes'])
            return {'id': item['id'], 'path': item['path'], 'url': item['url'], 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'hash_authority': 'fixture', 'acquired_at': '2026-01-01T00:00:00+00:00', 'state': 'downloaded_verified', 'inspection': None}
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'acquire', fake_acquire):
            module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True)
        reviews_path = self.root / 'configs/dataset_download_reviews.json'; reviews = json.loads(reviews_path.read_text())
        entry = next(r for r in reviews['reviews'] if r['id'] == self.dataset['id'])
        entry['full_release'].update(unsupported_sources=['https://doi.org/10.5281/zenodo.1'], complete=False); write(reviews_path, reviews)  # as an earlier resolver version left it
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'acquire', fake_acquire):
            again = module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True)
        self.assertEqual(again['counts'], {'already_complete': 1})
        entry = next(r for r in json.loads(reviews_path.read_text())['reviews'] if r['id'] == self.dataset['id'])
        self.assertEqual((entry['full_release']['complete'], entry['full_release']['unsupported_sources'], entry['minimal_selection']), (True, [], 'All files (complete provider release)'))
        usage = json.loads((self.root / 'configs/dataset_study_usage.json').read_text())['datasets'][0]
        self.assertEqual(usage['completeness'], 'complete_public_release', 'the registry label follows the confirmed complete resolution')

    def test_total_cap_counts_what_the_selection_already_holds_and_has_registered(self):
        write(self.instructions, self.spec)

        def fake_acquire(item, locked, base, execute, journal, license_context, **options):
            path = base / item['path']; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'x' * item['expected_bytes'])
            return {'id': item['id'], 'path': item['path'], 'url': item['url'], 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'hash_authority': 'fixture', 'acquired_at': '2026-01-01T00:00:00+00:00', 'state': 'downloaded_verified', 'inspection': None}
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'acquire', fake_acquire):
            module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True)
        self.assertEqual(module.committed_bytes(self.root, ['ZEN']), (1020, {}), 'held bytes of the selection count; nothing is reserved')
        self.assertEqual(module.committed_bytes(self.root, ['OTHER']), (0, {}))
        grown = {**self.record, 'files': self.record['files'] + [{'key': 'extra.csv', 'size': 5000, 'checksum': 'md5:' + 'e' * 32}]}
        self.web = FakeWeb({'https://zenodo.org/api/records/1': (200, json.dumps(grown).encode())})
        with patch.object(module, 'fetch', self.web.fetch):
            module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=False)  # registers the new file without transferring it
        self.assertEqual(module.committed_bytes(self.root, ['ZEN']), (1020, {'ZEN': 6000 + 4096}), 'a registered, unheld job reserves its budget')
        write(self.instructions, {**self.spec, 'max_total_bytes': 8000})  # the new file alone would fit; with the 1,020 bytes held it does not
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'acquire', fake_acquire):
            report = module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True)
        self.assertEqual(report['counts'], {'blocked_total_byte_budget': 1})
        outcome = report['outcomes'][0]
        self.assertEqual((outcome['selection_committed_bytes_before'], outcome['package_budget_bytes'], outcome['withdrawn_jobs']), (1020, 6000 + 4096, ['zenodo_1_extra_csv']),
                         'the reservation is replaced by the plan, the plan does not fit, and the size-blocked job is withdrawn')
        manifest = json.loads((self.root / 'configs/research_resources.json').read_text())['resources']
        self.assertEqual(sorted(r['path'] for r in manifest), ['datasets/zenodo_1/README.md', 'datasets/zenodo_1/data.zip'], 'held jobs stay registered')
        self.assertEqual(module.committed_bytes(self.root, ['ZEN']), (1020, {}))
        write(self.instructions, {**self.spec, 'max_total_bytes': 12000})
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'acquire', fake_acquire):
            report = module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True)
        self.assertEqual(report['counts'], {'ready': 1}, 'a cap that covers held plus new bytes lets the addition through')
        self.assertEqual(module.committed_bytes(self.root, ['ZEN']), (6020, {}))

    def test_fetch_retries_transient_provider_errors_and_returns_persistent_ones(self):
        import io, urllib.error
        calls = []

        def flaky(request, timeout):
            calls.append(request.full_url)
            if len(calls) < 3:
                raise urllib.error.HTTPError(request.full_url, 503, 'busy', {}, io.BytesIO(b''))
            response = io.BytesIO(b'{"ok": true}'); response.status = 200; response.geturl = lambda: request.full_url; response.headers = {}
            return response
        with patch.object(module.urllib.request, 'urlopen', flaky), patch.object(module.time, 'sleep', lambda s: None):
            status, _, _, body = module.fetch('https://provider.example/manifest')
        self.assertEqual((status, body, len(calls)), (200, b'{"ok": true}', 3), 'a momentary 503 is retried')
        calls.clear()

        def gone(request, timeout):
            calls.append(1); raise urllib.error.HTTPError(request.full_url, 404, 'missing', {}, io.BytesIO(b''))
        with patch.object(module.urllib.request, 'urlopen', gone), patch.object(module.time, 'sleep', lambda s: None):
            status, _, _, body = module.fetch('https://provider.example/manifest')
        self.assertEqual((status, body, len(calls)), (404, None, 1), 'a persistent error is returned at once')

    def test_a_run_can_be_restricted_to_named_datasets_of_the_instructions(self):
        write(self.instructions, self.spec)
        with patch.object(module, 'fetch', self.web.fetch):
            with self.assertRaisesRegex(ValueError, 'datasets_not_in_instructions'):
                module.run(self.instructions, root=self.root, only=['OTHER'])
            report = module.run(self.instructions, root=self.root, only=['ZEN'])
        self.assertEqual(([o['dataset'] for o in report['outcomes']], report['only']), (['ZEN'], ['ZEN']))

    def test_reuse_from_another_study_hands_its_holdings_to_the_acquirer(self):
        write(self.instructions, self.spec)
        other = self.root / 'studies' / 'other-study'
        (other / 'data/acquisitions/datasets/zenodo_1').mkdir(parents=True); (other / 'configs').mkdir()
        (other / 'data/acquisitions/datasets/zenodo_1/data.zip').write_bytes(b'x' * 1000)
        write(other / 'configs/research_resources.lock.json', {'files': [{'id': 'zenodo_1_data_zip', 'path': 'datasets/zenodo_1/data.zip', 'url': 'https://zenodo.org/api/records/1/files/data.zip/content', 'bytes': 1000, 'sha256': hashlib.sha256(b'x' * 1000).hexdigest()}]})
        seen = {}

        def fake_acquire(item, locked, base, execute, journal, license_context, **options):
            seen[item['id']] = (options.get('reuse') or {}).get(item['url'])
            path = base / item['path']; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'x' * item['expected_bytes'])
            record = {'id': item['id'], 'path': item['path'], 'url': item['url'], 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'hash_authority': 'fixture', 'acquired_at': '2026-01-01T00:00:00+00:00', 'state': 'downloaded_verified', 'inspection': None}
            return {**record, 'copied_from': {'study': 'other-study'}} if seen[item['id']] else record
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'acquire', fake_acquire):
            with self.assertRaisesRegex(ValueError, 'reuse_study_is_this_study'):
                module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True, reuse_from=[str(self.root)])
            report = module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True, reuse_from=[str(other)])
        self.assertEqual(report['execution']['copied_from_other_studies'], 1)
        self.assertEqual({k: (v or {}).get('study') for k, v in seen.items()}, {'zenodo_1_data_zip': 'other-study', 'zenodo_1_readme_md': None}, 'only the file the other study holds is offered for reuse')
        rows = [json.loads(l) for l in (self.root / report['run'] / 'operations.jsonl').read_text().splitlines()]
        self.assertEqual(rows[0]['details']['reuse_from'], ['other-study'])

    def test_saved_reuse_option_finds_the_other_study_workspaces_of_this_checkout(self):
        write(self.instructions, {**self.spec, 'reuse_held_files': True})
        other = self.root / 'studies' / 'other-study'
        (other / 'data/acquisitions/datasets/zenodo_1').mkdir(parents=True); (other / 'configs').mkdir()
        (other / 'data/acquisitions/datasets/zenodo_1/data.zip').write_bytes(b'x' * 1000)
        write(other / 'configs/research_resources.lock.json', {'files': [{'id': 'zenodo_1_data_zip', 'path': 'datasets/zenodo_1/data.zip', 'url': 'https://zenodo.org/api/records/1/files/data.zip/content', 'bytes': 1000, 'sha256': hashlib.sha256(b'x' * 1000).hexdigest()}]})
        (self.root / 'studies' / 'empty-study' / 'configs').mkdir(parents=True)  # a workspace without a lock holds nothing to reuse
        seen = {}

        def fake_acquire(item, locked, base, execute, journal, license_context, **options):
            seen[item['id']] = (options.get('reuse') or {}).get(item['url'])
            path = base / item['path']; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'x' * item['expected_bytes'])
            record = {'id': item['id'], 'path': item['path'], 'url': item['url'], 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'hash_authority': 'fixture', 'acquired_at': '2026-01-01T00:00:00+00:00', 'state': 'downloaded_verified', 'inspection': None}
            return {**record, 'copied_from': {'study': 'other-study'}} if seen[item['id']] else record
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'acquire', fake_acquire):
            report = module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True)
        self.assertEqual(report['execution']['copied_from_other_studies'], 1)
        self.assertEqual({k: (v or {}).get('study') for k, v in seen.items()}, {'zenodo_1_data_zip': 'other-study', 'zenodo_1_readme_md': None})
        rows = [json.loads(l) for l in (self.root / report['run'] / 'operations.jsonl').read_text().splitlines()]
        self.assertEqual((rows[0]['details']['reuse_from'], rows[0]['details']['reuse_held_files']), (['other-study'], True), 'the saved option names the workspaces it found')

    def reroute(self, route):
        """The fixture dataset under another access route; the receipt and the instructions follow it."""
        self.dataset['access_route'] = route
        selection = self.root / 'data/interim/dataset_selection/20260101T000000000000Z/selection.json'
        write(selection, {'datasets': [self.dataset]})
        sha = hashlib.sha256(selection.read_bytes()).hexdigest()
        write(self.root / 'data/interim/dataset_selection/latest.json', {'selection': str(selection.relative_to(self.root)), 'selection_sha256': sha})
        self.spec['catalogue_sha256'] = sha
        write(self.instructions, self.spec)
        write(self.root / 'configs/dataset_study_usage.json', {'datasets': [{'id': 'ZEN', 'name': 'Zenodo set', 'source_urls': ['https://zenodo.org/records/1'], 'paths': [], 'completeness': 'not_obtained', 'missing_components': [], 'next_action': ''}]})

    def user_folder(self, checksums=True):
        folder = self.root / 'downloads' / 'zen'
        (folder / 'sub').mkdir(parents=True)
        (folder / 'data.csv').write_text('a,b\n1,2\n')
        (folder / 'sub/README.md').write_text('# readme\n')
        (folder / 'notes.txt').write_text('notes\n')
        if checksums:  # the provider's checksum file as the download brought it along; notes.txt is corrupt
            (folder / 'SHA256SUMS.txt').write_text(''.join(f"{hashlib.sha256((folder / n).read_bytes()).hexdigest()}  {n}\n" for n in ('data.csv', 'sub/README.md')) + '0' * 64 + '  ./notes.txt\n')
        return folder

    def test_import_from_a_user_folder_verifies_against_its_checksum_file_without_touching_the_provider(self):
        self.reroute('registration_credentialed_access')  # the user obtained the files through the route; it no longer blocks them
        folder = self.user_folder()
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'ROOT', self.root):
            report = module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True, import_from={'ZEN': str(folder)})
        self.assertEqual(self.web.calls, [], 'an imported release never contacts the provider')
        outcome = report['outcomes'][0]
        self.assertEqual((outcome['outcome'], outcome['release_files'], outcome['new_jobs'], outcome['imported_from'], outcome['manifest']),
                         ('ready', 3, 3, str(folder), 'SHA256SUMS.txt supplied with the files'))
        self.assertEqual(report['execution'], {'acquired': 2, 'failed': 1, 'quarantined': 0, 'lock_files': 2, 'copied_from_other_studies': 0, 'imported_from_folders': 2})
        lock = {f['path']: f for f in json.loads((self.root / 'configs/research_resources.lock.json').read_text())['files']}
        self.assertEqual(sorted(lock), ['datasets/zen/data.csv', 'datasets/zen/sub/README.md'])
        self.assertEqual((lock['datasets/zen/data.csv']['hash_authority'], lock['datasets/zen/data.csv']['copied_from']['folder'], lock['datasets/zen/data.csv']['url']),
                         ('provider_SHA256_from_checksum_file_supplied_with_the_import', str(folder), 'https://zenodo.org/records/1/data.csv'))
        self.assertEqual((self.root / 'data/acquisitions/datasets/zen/data.csv').read_text(), 'a,b\n1,2\n')
        results = {r['id']: r for r in json.loads((self.root / 'data/acquisitions/acquisition-report.json').read_text())['results']}
        self.assertIn('provider_sha256_mismatch', next(r for r in results.values() if r['state'] == 'failed')['problem'], 'a corrupt download is refused, not locked')
        self.assertFalse((self.root / 'data/acquisitions/datasets/zen/notes.txt').exists())
        self.assertTrue(outcome['evidence'][0]['path'].endswith('import_zen_SHA256SUMS.txt'), 'the checksum file is retained as evidence')
        rows = [json.loads(l) for l in (self.root / report['run'] / 'operations.jsonl').read_text().splitlines()]
        self.assertEqual(rows[0]['details']['import_from'], {'ZEN': str(folder)})
        self.assertTrue(any(r['kind'] == 'transfer_started' and r['details'].get('imported_from_folder') == str(folder) for r in rows))
        log = [json.loads(l) for l in (self.root / 'actions.jsonl').read_text().splitlines()]
        self.assertIn(f'2 imported from ZEN ({folder})', log[-1]['detail'])
        entry = json.loads((self.root / 'configs/dataset_study_usage.json').read_text())['datasets'][0]
        self.assertEqual(entry['paths'], ['data/acquisitions/datasets/zen/'])

    def test_import_without_a_checksum_file_needs_the_missing_checksum_acknowledgement(self):
        self.reroute('registration_data_use_agreement')
        folder = self.user_folder(checksums=False)
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'ROOT', self.root):
            report = module.run(self.instructions, root=self.root, register_jobs=True, import_from={'ZEN': str(folder)})
            self.assertEqual((report['outcomes'][0]['outcome'], report['outcomes'][0]['unacknowledged_files']), ('needs_technical_risk_acknowledgement', 3))
            self.assertEqual(report['outcomes'][0]['manifest'], 'none: local SHA-256 identities only (no checksum file supplied)')
            write(self.instructions, {**self.spec, 'technical_risk_acknowledgements': ['missing_provider_checksum'], 'direct_holdings_for_acknowledged_risks': True})
            report = module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True, import_from={'ZEN': str(folder)})
        self.assertEqual(self.web.calls, [])
        self.assertEqual(report['execution'], {'acquired': 3, 'failed': 0, 'quarantined': 0, 'lock_files': 3, 'copied_from_other_studies': 0, 'imported_from_folders': 3})
        lock = {f['path']: f for f in json.loads((self.root / 'configs/research_resources.lock.json').read_text())['files']}
        self.assertEqual({f['hash_authority'] for f in lock.values()}, {'local_first_acquisition_SHA256_not_provider_checksum'})
        self.assertEqual({f['identity'] for f in lock.values()}, {'local_sha256_only'})

    def test_import_is_refused_for_an_incomplete_folder_or_one_inside_the_workspace(self):
        self.reroute('registration_credentialed_access')
        folder = self.user_folder()
        (folder / 'SHA256SUMS.txt').write_text((folder / 'SHA256SUMS.txt').read_text() + '1' * 64 + '  missing.csv\n')
        with patch.object(module, 'fetch', self.web.fetch):
            report = module.run(self.instructions, root=self.root, register_jobs=True, import_from={'ZEN': str(folder)})
            self.assertEqual((report['outcomes'][0]['outcome'], report['outcomes'][0]['missing_files']), ('import_folder_incomplete_1_listed_files_missing', ['missing.csv']))
            self.assertEqual(json.loads((self.root / 'configs/research_resources.json').read_text())['resources'], [], 'nothing is registered from an incomplete release')
            inside = self.root / 'data/acquisitions/stray'
            inside.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'import_folder_inside_the_workspace_data'):
                module.run(self.instructions, root=self.root, import_from={'ZEN': str(inside)})
            with self.assertRaisesRegex(ValueError, 'import_dataset_not_in_instructions'):
                module.run(self.instructions, root=self.root, import_from={'OTHER': str(folder)})
        self.assertEqual(self.web.calls, [])

    def test_direct_holdings_option_locks_acknowledged_risk_files_with_recorded_caveats(self):
        record = {'metadata': {'access_right': 'open'}, 'files': [{'key': 'data.zip', 'size': 1000, 'checksum': 'md5:' + 'c' * 32}, {'key': 'notes.txt', 'size': 20}]}
        self.web = FakeWeb({'https://zenodo.org/api/records/1': (200, json.dumps(record).encode())})
        write(self.instructions, {**self.spec, 'technical_risk_acknowledgements': ['missing_provider_checksum'], 'direct_holdings_for_acknowledged_risks': True})
        write(self.root / 'configs/dataset_study_usage.json', {'datasets': [{'id': 'ZEN', 'name': 'Zenodo set', 'source_urls': ['https://zenodo.org/records/1'], 'paths': [], 'completeness': 'not_obtained', 'missing_components': [], 'next_action': ''}]})

        def fake_acquire(item, locked, base, execute, journal, license_context, **options):
            path = base / item['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * (item.get('expected_bytes') or 1))
            return {'id': item['id'], 'path': item['path'], 'url': item['url'], 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'hash_authority': 'local_first_acquisition_SHA256_not_provider_checksum' if 'provider_md5' not in item else 'provider_MD5_plus_local_SHA256', 'acquired_at': '2026-01-01T00:00:00+00:00', 'state': 'downloaded_verified', 'inspection': None}
        with patch.object(module, 'fetch', self.web.fetch), patch.object(module.acquisition, 'acquire', fake_acquire):
            report = module.run(self.instructions, root=self.root, register_jobs=True, execute_jobs=True)
        self.assertEqual(report['execution'], {'acquired': 2, 'failed': 0, 'quarantined': 0, 'lock_files': 2})
        self.assertFalse((self.root / 'data/quarantine').exists())
        results = {r['id']: r for r in json.loads((self.root / 'data/acquisitions/acquisition-report.json').read_text())['results']}
        notes = next(r for r in results.values() if r['path'].endswith('notes.txt'))
        self.assertEqual((notes['identity'], notes['technical_risks']), ('local_sha256_only', ['missing_provider_checksum']))
        entry = json.loads((self.root / 'configs/dataset_study_usage.json').read_text())['datasets'][0]
        self.assertEqual(entry['completeness'], 'complete_public_release')
        self.assertTrue(any(c.startswith('Identity: 1 held file') for c in entry['data_caveats']))
        self.assertTrue(any(c.startswith('Version:') for c in entry['data_caveats']))


if __name__ == '__main__':
    unittest.main()
