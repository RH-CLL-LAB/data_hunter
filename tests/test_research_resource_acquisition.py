"""Offline checks for immutable acquisition, provenance and archive boundaries."""
import hashlib
import gzip
import importlib.util
from pathlib import Path
import tempfile
import tarfile
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location(
    'research_acquisition', Path(__file__).parents[1] / 'Scripts/acquire_research_resources.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ResearchAcquisitionTests(unittest.TestCase):
    def setUp(self):
        # Transfer/format tests isolate the preflight; its behavior is exercised
        # separately below with an actual temporary review registry.
        preflight = patch.object(module, 'require_download_review', return_value={'basis':'test_approved'})
        preflight.start()
        self.addCleanup(preflight.stop)
        rights = patch.object(module, 'transfer_license_preflight', return_value={'status': 'allowed', 'basis': 'fixture_rights'})
        rights.start(); self.addCleanup(rights.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.payload = b'original source bytes'
        self.item = {'id': 'example', 'acquisition': 'direct', 'path': 'sample.txt',
                     'url': 'https://example.org/sample.txt', 'format': 'text',
                     'provider_md5': hashlib.md5(self.payload).hexdigest()}

    def test_active_manifest_cannot_reintroduce_excluded_or_unreviewed_resources(self):
        policy = self.root / 'configs/resource_scope.json'
        policy.parent.mkdir()
        policy.write_text(json.dumps({'decisions': [
            {'id': 'clinical', 'retain': True}, {'id': 'general', 'retain': False}]}))
        with patch.object(module, 'ROOT', self.root):
            module.validate_scope({'resources': [{'id': 'clinical'}]})
            for identity in ('general', 'unreviewed'):
                with self.assertRaisesRegex(ValueError, 'outside_reviewed_scope'):
                    module.validate_scope({'resources': [{'id': identity}]})

    def test_plan_and_access_blocks_never_transfer(self):
        with patch.object(module.subprocess, 'run', side_effect=AssertionError('network')):
            result = module.acquire(self.item, {}, self.root)
            self.assertEqual(result['state'], 'planned')
            result = module.acquire({'id': 'controlled', 'acquisition': 'blocked_access',
                                     'reason': 'DUA required'}, {}, self.root, execute=True)
            self.assertEqual(result['state'], 'blocked_access')

    def transfer(self, payload, events):
        def fake(command, **kwargs):
            self.assertEqual(events[0][0], 'transfer_started')
            self.assertIn('--proto-redir', command)
            Path(command[command.index('--output') + 1]).write_bytes(payload)
            return SimpleNamespace(returncode=0)
        return fake

    def test_transfer_checks_provider_digest_and_records_identity(self):
        events = []
        with patch.object(module.subprocess, 'run', side_effect=self.transfer(self.payload, events)):
            record = module.acquire(self.item, {}, self.root, execute=True,
                                    journal=lambda *args: events.append(args))
        self.assertEqual(record['sha256'], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual((self.root / 'sample.txt').read_bytes(), self.payload)
        self.assertEqual(record['hash_authority'], 'provider_MD5_plus_local_SHA256')

    def test_transfer_uses_a_stall_timeout_instead_of_a_fixed_cap(self):
        events, commands = [], []
        fake = self.transfer(self.payload, events)
        with patch.object(module.subprocess, 'run', side_effect=lambda command, **kwargs: (commands.append(command), fake(command, **kwargs))[1]):
            module.acquire(self.item, {}, self.root, execute=True, journal=lambda *args: events.append(args))
        command = commands[0]
        self.assertNotIn('--max-time', command, 'a full release may take longer than any fixed cap; only a stalled connection fails')
        self.assertNotIn('--continue-at', command, 'a first attempt starts from the beginning')
        for flag, value in (('--connect-timeout', '20'), ('--speed-time', '120'), ('--speed-limit', '1024')):
            self.assertEqual(command[command.index(flag) + 1], value)

    def test_a_dropped_transfer_resumes_from_its_partial_and_stops_without_progress(self):
        events, commands = [], []

        def flaky(command, **kwargs):
            commands.append(command)
            out = Path(command[command.index('--output') + 1])
            if len(commands) == 1:
                out.write_bytes(self.payload[:8])
                return SimpleNamespace(returncode=56, stderr='curl: (56) Recv failure')
            self.assertEqual(command[command.index('--continue-at') + 1], '-', 'the second attempt resumes the retained partial')
            out.write_bytes(self.payload)
            return SimpleNamespace(returncode=0, stderr='')
        with patch.object(module.subprocess, 'run', side_effect=flaky), patch.object(module.time, 'sleep', lambda seconds: None):
            record = module.acquire(self.item, {}, self.root, execute=True, journal=lambda *args: events.append(args))
        self.assertEqual((len(commands), record['state'], (self.root / 'sample.txt').read_bytes()), (2, 'downloaded_verified', self.payload))
        (self.root / 'sample.txt').unlink()
        commands.clear()

        def dead(command, **kwargs):
            commands.append(command)
            Path(command[command.index('--output') + 1]).write_bytes(b'')
            return SimpleNamespace(returncode=28, stderr='curl: (28) Operation too slow')
        with patch.object(module.subprocess, 'run', side_effect=dead), patch.object(module.time, 'sleep', lambda seconds: None):
            with self.assertRaisesRegex(RuntimeError, 'curl_exit_28'):
                module.acquire(self.item, {}, self.root, execute=True, journal=lambda *args: events.append(args))
        self.assertEqual(len(commands), 1, 'an attempt that made no progress is not repeated')

    def test_bytes_another_study_holds_are_copied_and_verified_instead_of_downloaded(self):
        other = self.root / 'other-study'
        (other / 'data/acquisitions').mkdir(parents=True); (other / 'configs').mkdir()
        (other / 'data/acquisitions/sample.txt').write_bytes(self.payload)
        (other / 'configs/research_resources.lock.json').write_text(json.dumps({'files': [{'id': 'example', 'path': 'sample.txt', 'url': self.item['url'], 'bytes': len(self.payload), 'sha256': hashlib.sha256(self.payload).hexdigest()}]}))
        reuse = module.holdings_of([other])
        self.assertEqual(list(reuse), [self.item['url']]); self.assertEqual(reuse[self.item['url']]['study'], 'other-study')
        events = []
        with patch.object(module.subprocess, 'run', side_effect=AssertionError('no download when the bytes are held elsewhere')):
            record = module.acquire(self.item, {}, self.root, execute=True, journal=lambda *args: events.append(args), reuse=reuse)
        self.assertEqual((record['state'], record['copied_from']['study'], record['hash_authority']), ('downloaded_verified', 'other-study', 'provider_MD5_plus_local_SHA256'))
        self.assertEqual((self.root / 'sample.txt').read_bytes(), self.payload)
        self.assertEqual(events[0][1].get('reuse_from_study'), 'other-study')
        (self.root / 'sample.txt').unlink()
        (other / 'data/acquisitions/sample.txt').write_bytes(b'changed since it was locked')  # the source no longer matches its own lock
        events.clear()
        with patch.object(module.subprocess, 'run', side_effect=self.transfer(self.payload, events)):
            record = module.acquire(self.item, {}, self.root, execute=True, journal=lambda *args: events.append(args), reuse=reuse)
        self.assertEqual((record['state'], 'copied_from' in record), ('downloaded_verified', False), 'rejected bytes fall back to a download')
        self.assertIn('reuse_rejected', [ev[0] for ev in events])
        (self.root / 'sample.txt').unlink()
        with patch.object(module.subprocess, 'run', side_effect=AssertionError('a copy-only run never downloads')):
            with self.assertRaisesRegex(ValueError, 'copy_only_run_has_no_verified_copy_to_reuse'):
                module.acquire(self.item, {}, self.root, execute=True, journal=lambda *args: events.append(args), reuse=reuse, reuse_only=True)

    def test_json_label_is_a_caveat_not_a_refusal_once_the_provider_identity_verified(self):
        jsonc = b'{\n  // a comment\n  "a": 1\n}\n'
        item = {**self.item, 'format': 'json', 'provider_md5': hashlib.md5(jsonc).hexdigest()}
        events = []
        with patch.object(module.subprocess, 'run', side_effect=self.transfer(jsonc, events)):
            record = module.acquire(item, {}, self.root, execute=True, journal=lambda *args: events.append(args))
        self.assertEqual(record['state'], 'downloaded_verified'); self.assertIn('Expecting property name', record['format_caveat'])
        (self.root / 'sample.txt').unlink()
        lines = b'{"a": 1}\n{"a": 2}\n'
        item = {**self.item, 'format': 'json', 'provider_md5': hashlib.md5(lines).hexdigest()}
        with patch.object(module.subprocess, 'run', side_effect=self.transfer(lines, events)):
            record = module.acquire(item, {}, self.root, execute=True, journal=lambda *args: events.append(args))
        self.assertEqual((record['inspection'], 'format_caveat' in record), ({'format': 'json-lines', 'records': 2}, False))
        (self.root / 'sample.txt').unlink()
        item = {k: v for k, v in self.item.items() if k != 'provider_md5'}; item['format'] = 'json'
        with patch.object(module.subprocess, 'run', side_effect=self.transfer(jsonc, events)):
            with self.assertRaisesRegex(ValueError, 'Expecting property name'):
                module.acquire(item, {}, self.root, execute=True, journal=lambda *args: events.append(args))

    def test_bad_transfer_retains_partial_without_promoting(self):
        events = []
        with patch.object(module.subprocess, 'run', side_effect=self.transfer(b'bad', events)):
            with self.assertRaisesRegex(ValueError, 'provider_md5_mismatch'):
                module.acquire(self.item, {}, self.root, execute=True,
                               journal=lambda *args: events.append(args))
        self.assertFalse((self.root / 'sample.txt').exists())
        self.assertEqual((self.root / 'sample.txt.part').read_bytes(), b'bad')

    def test_locked_corruption_is_never_overwritten(self):
        path = self.root / 'sample.txt'
        path.write_bytes(b'corrupted')
        locked = {self.item['id']: {**self.item, 'sha256': hashlib.sha256(self.payload).hexdigest(),
                                    'bytes': len(self.payload)}}
        with patch.object(module.subprocess, 'run', side_effect=AssertionError('network')):
            with self.assertRaisesRegex(ValueError, 'locked_bytes_changed'):
                module.acquire(self.item, locked, self.root, execute=True)
        self.assertEqual(path.read_bytes(), b'corrupted')

    def test_missing_locked_file_cannot_be_replaced_with_changed_bytes(self):
        item = {k: v for k, v in self.item.items() if k != 'provider_md5'}
        locked = {item['id']: {**item, 'bytes': len(self.payload),
                               'sha256': hashlib.sha256(self.payload).hexdigest(), 'acquired_at': 'original'}}
        events = []
        with patch.object(module.subprocess, 'run', side_effect=self.transfer(b'new', events)):
            with self.assertRaisesRegex(ValueError, 'restored_bytes_differ'):
                module.acquire(item, locked, self.root, execute=True,
                               journal=lambda *args: events.append(args))

    def test_inventory_reports_ragged_rows_without_exporting_or_fixing(self):
        archive = self.root / 'data.zip'
        with zipfile.ZipFile(archive, 'w') as target:
            target.writestr('table.csv', 'id,value\n1,2\n3\n')
        before = archive.read_bytes()
        report = module.inspect_zip(archive)
        self.assertEqual(report['tables'][0]['ragged_rows'], 1)
        self.assertEqual(report['tables'][0]['rows'], 2)
        self.assertFalse(report['extracted'])
        self.assertEqual(before, archive.read_bytes())
        self.assertEqual(list(self.root.iterdir()), [archive])

    def test_archive_traversal_rejected(self):
        archive = self.root / 'data.zip'
        with zipfile.ZipFile(archive, 'w') as target:
            target.writestr('../escape.csv', 'id\n1\n')
        with self.assertRaisesRegex(ValueError, 'Unsafe relative path'):
            module.inspect_zip(archive)

    def test_archive_duplicate_names_rejected(self):
        import warnings
        archive = self.root / 'data.zip'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(archive, 'w') as target:
                target.writestr('same.csv', 'id\n1\n')
                target.writestr('same.csv', 'id\n2\n')
        with self.assertRaisesRegex(ValueError, 'duplicate_archive_member'):
            module.inspect_zip(archive)

    def test_configured_profile_keeps_duplicate_and_missing_findings(self):
        archive = self.root / 'data.zip'
        with zipfile.ZipFile(archive, 'w') as target:
            target.writestr('labs.csv', 'id,name,value\n1,HEMOGL,2\n1,HEMOGL,NULL\n')
        profile = {'member': 'labs.csv', 'distinct_groups': {'ids': ['id']},
                   'candidate_key': 'ids', 'required_fields': ['value'],
                   'analyte_field': 'name', 'analyte_pattern': 'hemo'}
        report = module.inspect_zip(archive, profile)['tables'][0]['deterministic_profile']
        self.assertEqual(report['distinct_counts'], {'ids': 1})
        self.assertEqual(report['repeated_candidate_keys'], 1)
        self.assertEqual(report['configured_missing_token_counts'], {'value': 1})
        self.assertEqual(report['candidate_analyte_name_counts'], {'HEMOGL': 2})
        self.assertFalse(report['clinical_mapping_validated'])

    def test_gzip_corruption_is_rejected(self):
        path = self.root / 'data.gz'
        path.write_bytes(gzip.compress(b'original records')[:-4])
        with self.assertRaises((EOFError, OSError)):
            module.validate(path, {'format': 'gzip'})

    def test_tar_archive_traversal_is_rejected_without_extraction(self):
        path = self.root / 'data.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            info = tarfile.TarInfo('../outside')
            info.size = 3
            archive.addfile(info, io.BytesIO(b'bad'))
        with self.assertRaisesRegex(ValueError, 'Unsafe relative path'):
            module.validate(path, {'format': 'tar.gz'})
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_provider_sha256_rejects_changed_lfs_content(self):
        path = self.root / 'data.txt'
        path.write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError, 'provider_sha256_mismatch'):
            module.validate(path, {'format': 'binary', 'provider_sha256': hashlib.sha256(b'right').hexdigest()})


class DownloadReviewTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);(self.root/'configs').mkdir()
        root=patch.object(module,'ROOT',self.root);root.start();self.addCleanup(root.stop)
        self.item={'id':'clinical','url':'https://provider.example/clinical.zip','path':'clinical.zip',
                   'kind':'dataset','format':'zip','max_bytes':1000,'acquisition':'direct'}

    def approve(self):
        evidence=self.root/'dictionary.txt';evidence.write_text('subject, test, value, unit, time')
        approval={**self.item,'decision':'approve_minimal_subset','use_case':'Verify subject/value/unit/time extraction',
                  'file_selection_reason':'Only clinical observations; omit images',
                  'metadata_evidence':[{'path':'dictionary.txt','sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}],
                  'checks':dict.fromkeys(['clinical_content','subject_and_grain','value_units_codes','time_semantics',
                                         'format_and_size','duplicate_overlap','test_gap','access'],True)}
        (self.root/'configs/dataset_download_reviews.json').write_text(json.dumps({'approved_artifacts':[approval]}))

    def test_unreviewed_bulk_is_blocked_before_network_or_partial_creation(self):
        with patch.object(module.subprocess,'run',side_effect=AssertionError('network must not run')):
            with self.assertRaisesRegex(ValueError,'metadata_preflight_required'):
                module.acquire(self.item,{},self.root,execute=True)
        self.assertFalse((self.root/'clinical.zip.part').exists())

    def test_metadata_approval_alone_does_not_allow_a_payload_transfer(self):
        self.approve()
        with patch.object(module,'transfer_license_preflight',return_value={'status':'blocked'}), patch.object(module.subprocess,'run',side_effect=AssertionError('network must not run')):
            with self.assertRaisesRegex(ValueError,'dataset_license_required_before_transfer'):
                module.acquire(self.item,{},self.root,execute=True)
        self.assertFalse((self.root/'clinical.zip.part').exists())

    def test_software_rights_cannot_license_a_clinical_payload(self):
        registry=SimpleNamespace(mode='lenient',study_id='medscrape',resources={'software':{'scope':'software_only'}},by_hash={'a'*64:{'software'}})
        approved={**self.item,'download_license_resources':['software']}
        result=module.transfer_license_preflight(self.item,{}, {'approved_artifacts':[approved]},registry)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason'],'software_license_is_not_data_rights')
        result=module.transfer_license_preflight({**self.item,'provider_sha256':'a'*64},{},registry=registry)
        self.assertEqual(result['status'],'blocked')

    def test_source_refusal_before_acquisition_blocks_an_old_download_binding(self):
        (self.root/'configs/dataset_license_reviews.json').write_text(json.dumps({'reviews':[
            {'dataset_id':'SOURCE','study_id':'medscrape','applied':True,'assets':[],'status':'prohibited'}]}))
        registry=SimpleNamespace(mode='lenient',study_id='medscrape',resources={'rights':{'catalog_ids':['SOURCE']}})
        binding={**self.item,'download_license_resources':['rights']}
        result=module.transfer_license_preflight(self.item,{}, {'approved_artifacts':[binding]},registry)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason'],'manual_source_prohibition_before_acquisition')

    def test_review_is_bound_to_metadata_and_exact_format_budget_url(self):
        self.approve()
        module.require_download_review(self.item,{})
        for field,value in [('url','https://provider.example/full.zip'),('format','tar'),('max_bytes',1000000)]:
            with self.assertRaisesRegex(ValueError,'identity_or_budget_changed'):
                module.require_download_review({**self.item,field:value},{})
        (self.root/'dictionary.txt').write_text('different schema')
        with self.assertRaisesRegex(ValueError,'metadata_missing_or_changed'):
            module.require_download_review(self.item,{})

    def test_review_registry_is_parsed_once_until_its_bytes_change(self):
        self.approve();p=self.root/'configs/dataset_download_reviews.json'
        first=module.require_download_review(self.item,{})
        self.assertEqual(first['review_registry_sha256'],hashlib.sha256(p.read_bytes()).hexdigest())
        self.assertIs(module.parsed_json(p)[0],module.parsed_json(p)[0],'an unchanged registry is parsed once per process')
        r=json.loads(p.read_text());r['approved_artifacts'][0]['max_bytes']=2000;p.write_text(json.dumps(r))
        with self.assertRaisesRegex(ValueError,'identity_or_budget_changed'):
            module.require_download_review(self.item,{})
        again=module.require_download_review({**self.item,'max_bytes':2000},{})
        self.assertEqual(again['review_registry_sha256'],hashlib.sha256(p.read_bytes()).hexdigest(),'a changed registry is re-read and re-hashed')

    def test_checklist_without_access_cannot_approve_bulk(self):
        self.approve();p=self.root/'configs/dataset_download_reviews.json';r=json.loads(p.read_text())
        r['approved_artifacts'][0]['checks']['access']=False;p.write_text(json.dumps(r))
        with self.assertRaisesRegex(ValueError,'incomplete_or_not_approved'):
            module.require_download_review(self.item,{})

    def test_reference_route_is_bounded_and_cannot_be_used_for_dataset_kind(self):
        module.require_download_review({**self.item,'kind':'specification_reference','max_bytes':1024},{})
        for item in [self.item,{**self.item,'kind':'specification_reference','max_bytes':6*1024**2}]:
            with self.assertRaisesRegex(ValueError,'metadata_preflight_required'):
                module.require_download_review(item,{})


class NearbyWorkspacesTests(unittest.TestCase):
    def test_workspaces_with_holdings_near_a_study_are_its_siblings_with_a_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            studies = Path(temporary) / 'studies'
            for name, lock in [('this-study', True), ('other-study', True), ('empty-study', False)]:
                (studies / name / 'configs').mkdir(parents=True)
                if lock:
                    (studies / name / 'configs/research_resources.lock.json').write_text('{"files": []}')
            (studies / 'notes.txt').write_text('not a workspace')
            self.assertEqual([p.name for p in module.workspaces_with_holdings_near(studies / 'this-study')], ['other-study'])
            self.assertEqual([p.name for p in module.workspaces_with_holdings_near(Path(temporary))], ['other-study', 'this-study'], 'a repository-root workspace sees its studies/ folder')
            self.assertEqual([p.name for p in module.workspaces_with_holdings_near(studies / 'empty-study')], ['other-study', 'this-study'])


if __name__ == '__main__':
    unittest.main()
