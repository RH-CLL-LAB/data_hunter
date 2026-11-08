"""Offline integration checks for selection, non-waivable gates and run records."""
import hashlib
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('download_agent',ROOT/'Scripts/run_download_agent.py')
agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)


class DownloadAgentTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.job={'id':'reviewed_table','acquisition':'direct','kind':'dataset','format':'json',
                  'url':'https://provider.example/minimal.json','path':'minimal.json','max_bytes':1024}
        self.spec={'schema_version':1,'kind':'datahunter_download_instructions','catalogue_sha256':'abc',
                   'dataset_ids':['A'],'license_mode':'lenient','max_bytes_per_artifact':1024,'max_total_bytes':2048,
                   'technical_risk_acknowledgements':[]}
        self.events=[];self.used=[0]
        self.patch=patch.object(agent.acquisition,'ROOT',self.root)
        self.patch.start();self.addCleanup(self.patch.stop)

    def write(self,path,data):
        p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(data));return p

    def approve(self,access=True):
        metadata=self.root/'dictionary.txt';metadata.write_text('subject, test, value, unit, time')
        approval={**self.job,'decision':'approve_minimal_subset','use_case':'clinical extraction',
                  'file_selection_reason':'Only required minimal observations',
                  'metadata_evidence':[{'path':'dictionary.txt','sha256':agent.sha(metadata)}],
                  'checks':dict.fromkeys(['clinical_content','subject_and_grain','value_units_codes','time_semantics',
                                         'format_and_size','duplicate_overlap','test_gap','access'],True)}
        approval['checks']['access']=access
        self.write('configs/dataset_download_reviews.json',{'approved_artifacts':[approval]})

    def process(self,job=None,ack=(),execute=True,locked=None):
        return agent.process_job(job or self.job,locked or {},self.root/'stage',self.root/'original',
                                 set(ack),execute,self.spec,lambda *args:self.events.append(args),self.used,{'status':'allowed','mode':self.spec['license_mode']})

    def test_stale_ids_caps_and_unknown_exception_cannot_be_instructions(self):
        agent.validate_instructions(self.spec,'abc')
        for changes in [{'catalogue_sha256':'old'},{'dataset_ids':['A','A']},{'dataset_ids':['../A']},
                        {'max_total_bytes':True},{'max_bytes_per_artifact':0},
                        {'technical_risk_acknowledgements':['ignore_licence']},{'license_mode':'ignore'}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                agent.validate_instructions({**self.spec,**changes},'abc')

    def test_access_and_exact_metadata_reviews_cannot_be_waived(self):
        self.approve(access=False)
        with patch.object(agent.acquisition.subprocess,'run',side_effect=AssertionError('network')):
            self.assertEqual(self.process(ack=agent.RISKS)['status'],'blocked_review_or_access')
            self.assertEqual(self.process({**self.job,'acquisition':'registration'},ack=agent.RISKS)['status'],'blocked_access')
        self.approve();(self.root/'dictionary.txt').write_text('changed')
        self.assertEqual(self.process(ack=agent.RISKS)['status'],'blocked_review_or_access')

    def test_unknown_checksum_and_size_require_explicit_acknowledgements(self):
        self.approve()
        with patch.object(agent.acquisition.subprocess,'run',side_effect=AssertionError('network')):
            r=self.process();self.assertEqual(r['status'],'needs_technical_risk_acknowledgement')
            self.assertEqual(set(r['unaccepted']),set(agent.RISKS))
            r=self.process(ack=['missing_provider_checksum'])
            self.assertEqual(r['unaccepted'],['unknown_advertised_size'])
            r=self.process(ack=agent.RISKS,execute=False)
            self.assertEqual(r['status'],'eligible_dry_run')
            self.assertEqual(self.used,[1024])
        self.assertFalse((self.root/'stage').exists())

    def test_caps_and_https_remain_blocking_with_all_exceptions(self):
        self.approve();self.spec['max_bytes_per_artifact']=1023
        self.assertEqual(self.process(ack=agent.RISKS)['status'],'blocked_byte_budget')
        self.assertEqual(self.process({**self.job,'url':'http://provider.example/minimal.json'},ack=agent.RISKS)['status'],'blocked_https_required')
        self.spec['max_bytes_per_artifact']=1024;self.used[0]=2048
        self.assertEqual(self.process(ack=agent.RISKS)['status'],'blocked_byte_budget')

    def test_changed_locked_identity_or_bytes_are_never_replaced(self):
        p=self.root/'original/minimal.json';p.parent.mkdir();p.write_text('{}')
        old={**self.job,'bytes':2,'sha256':agent.sha(p)}
        locked={self.job['id']:old}
        with patch.object(agent.acquisition.subprocess,'run',side_effect=AssertionError('network')):
            self.assertEqual(self.process(locked=locked)['status'],'already_downloaded_verified')
            changed={**self.job,'url':'https://provider.example/different.json'}
            self.assertEqual(self.process(changed,locked=locked)['status'],'blocked_locked_source_identity_changed')
            p.write_text('changed')
            self.assertEqual(self.process(locked=locked,ack=agent.RISKS)['status'],'blocked_existing_bytes_changed')
        self.assertEqual(p.read_text(),'changed')

    def test_binding_requires_path_and_checksum_or_explicit_reviewed_alias(self):
        base=self.root/'original';base.mkdir()
        lock={self.job['id']:{**self.job,'sha256':'same'}}
        d={'id':'A','obtained_artifacts':[{'path':'other/minimal.json','sha256':'same'}]}
        manifest={'resources':[self.job]}
        self.assertEqual(agent.bound_jobs(d,manifest,lock,{},base,self.root),[])
        d['obtained_artifacts'][0]['path']='original/minimal.json'
        self.assertEqual(agent.bound_jobs(d,manifest,lock,{},base,self.root),[self.job])
        d['obtained_artifacts'][0]['sha256']='changed'
        self.assertEqual(agent.bound_jobs(d,manifest,lock,{},base,self.root),[])
        self.assertEqual(agent.bound_jobs(d,manifest,{}, {'reviews':[{'id':'A','resource_aliases':['reviewed_table','inactive']}]},base,self.root),[self.job])

    def fixture(self):
        self.approve();reviews=agent.read(self.root/'configs/dataset_download_reviews.json')
        reviews['reviews']=[{'id':id,'resource_aliases':['reviewed_table']} for id in ['A','B']]
        reviews['approved_artifacts'][0]['download_license_resources']=['table_rights']
        self.write('configs/dataset_download_reviews.json',reviews)
        self.write('configs/research_resources.json',{'resources':[self.job]})
        self.write('configs/research_resources.lock.json',{'files':[]})
        self.write('configs/resource_scope.json',{'decisions':[{'id':'reviewed_table','retain':True}]})
        notice=self.root/'notice.txt';notice.write_text('Test fixture rights evidence')
        self.write('configs/data_licenses.json',{'schema_version':1,'default':'lenient','resources':[
            {'id':'table_rights','license':'fixture grant','reviewed_on':'2026-01-01','review_due':'2099-01-01',
             'reason':'fixture grant with conditions','operations':{'local_inference':'allowed','local_embedding':'allowed'},
             'ai_permissions':{'local_inference':'unreviewed','local_embedding':'unreviewed'},
             'evidence':[{'snapshot':'notice.txt','sha256':agent.sha(notice)}]}]})
        self.write('configs/data_license_assets.json',{'schema_version':1,'files':[]})
        source=self.root/'Scripts/acquire_research_resources.py';source.parent.mkdir();source.write_text('# test fixture adapter snapshot')
        source=self.root/'src/datahunter/data_rights.py';source.parent.mkdir(parents=True);source.write_text('# test fixture policy implementation snapshot')
        catalogue=self.write('selection.json',{'datasets':[{'id':id,'obtained_artifacts':[],
                             'missing_components':['Independent reference review'],'access_route':'public',
                             'next_action':'Review reference'} for id in ['A','B','C']]})
        self.spec.update(dataset_ids=['A','B','C'],catalogue_sha256=agent.sha(catalogue),technical_risk_acknowledgements=list(agent.RISKS))
        self.write('data/interim/dataset_selection/latest.json',{'selection':'selection.json','selection_sha256':agent.sha(catalogue)})
        return self.write('instructions.json',self.spec)

    def test_end_to_end_deduplicates_transfer_keeps_originals_and_records_manual_work(self):
        instructions=self.fixture();lock=self.root/'configs/research_resources.lock.json';before=lock.read_bytes()
        def transfer(command,**kwargs):
            self.assertIn('--max-filesize',command)
            Path(command[command.index('--output')+1]).write_text('{"subject":"example"}')
            return SimpleNamespace(returncode=0)
        with patch.object(agent.acquisition.subprocess,'run',side_effect=transfer) as net:
            report=agent.run(instructions,execute=True,root=self.root)
        self.assertEqual(net.call_count,1)
        self.assertEqual(report['unique_artifacts'][0]['status'],'downloaded_quarantined')
        self.assertEqual(report['dataset_results'][2]['status'],'manual_access_or_metadata_review_required')
        self.assertFalse(report['ai_use_authorized_by_download'])
        self.assertEqual(lock.read_bytes(),before)
        delivered=self.root/report['quarantine_directory']/'minimal.json'
        self.assertEqual(delivered.read_text(),'{"subject":"example"}')
        run=next((self.root/'runs/downloads').iterdir())
        self.assertEqual(agent.read(run/'instructions.json'),self.spec)
        self.assertTrue((run/'evidence/configs/dataset_download_reviews.json').is_file())
        events=[json.loads(line) for line in (run/'operations.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['kind'],'run_finished')
        self.assertEqual(len([e for e in events if e['kind']=='dataset_outcome']),3)
        self.assertFalse((self.root/'data/raw').exists())
        study_log=[json.loads(l) for l in (self.root/'docs/research-studies/baseline/actions.jsonl').read_text().splitlines()]
        self.assertEqual(study_log[-1]['action'],'download_run');self.assertTrue(study_log[-1]['detail'].startswith('EXECUTED: scope minimal'))
        self.assertIn('downloaded_quarantined',study_log[-1]['detail']);self.assertEqual(study_log[-1]['run'],str(run.relative_to(self.root)))

    def test_dry_run_does_not_transfer_and_known_bad_checksum_never_promotes(self):
        instructions=self.fixture()
        with patch.object(agent.acquisition.subprocess,'run',side_effect=AssertionError('network')):
            report=agent.run(instructions,root=self.root)
        self.assertEqual(report['unique_artifacts'][0]['status'],'eligible_dry_run')
        self.assertFalse((self.root/report['quarantine_directory']).exists())
        self.assertTrue(json.loads((self.root/'docs/research-studies/baseline/actions.jsonl').read_text().splitlines()[-1])['detail'].startswith('Dry run'))
        self.job['provider_sha256']=hashlib.sha256(b'correct').hexdigest()
        self.write('configs/data_license_assets.json',{'schema_version':1,'files':[
            {'path':'reviewed/expected.json','sha256':self.job['provider_sha256'],'resource':'table_rights'}]})
        def bad_transfer(command,**kwargs):
            Path(command[command.index('--output')+1]).write_text('{}')
            return SimpleNamespace(returncode=0)
        with patch.object(agent.acquisition.subprocess,'run',side_effect=bad_transfer):
            r=self.process(ack=agent.RISKS)
        self.assertEqual(r['status'],'transfer_failed')
        self.assertIn('provider_sha256_mismatch',r['problem'])
        self.assertFalse((self.root/'stage/minimal.json').exists())
        self.assertTrue((self.root/'stage/minimal.json.part').is_file())

    def test_selected_license_mode_is_enforced_and_cannot_be_waived(self):
        instructions=self.fixture()
        with patch.object(agent.acquisition.subprocess,'run',side_effect=AssertionError('network')):
            lenient=agent.run(instructions,root=self.root)
            self.assertEqual(lenient['license_mode'],'lenient')
            self.assertEqual(lenient['unique_artifacts'][0]['status'],'eligible_dry_run')
            self.spec['license_mode']='restrictive';self.write('instructions.json',self.spec)
            strict=agent.run(instructions,execute=True,root=self.root)
            self.assertEqual(strict['unique_artifacts'][0]['status'],'blocked_license')
            self.assertEqual(strict['license_mode'],'restrictive')
            policy=agent.read(self.root/'configs/data_licenses.json')
            policy['resources'][0]['ai_permissions']={'local_inference':'prohibited','local_embedding':'prohibited'}
            self.write('configs/data_licenses.json',policy)
            self.spec['license_mode']='lenient';self.write('instructions.json',self.spec)
            banned=agent.run(instructions,execute=True,root=self.root)
            self.assertEqual(banned['unique_artifacts'][0]['status'],'blocked_license')
            policy['resources'][0]['ai_permissions']={'local_inference':'allowed','local_embedding':'allowed'}
            self.write('configs/data_licenses.json',policy)
            self.spec['license_mode']='restrictive';self.write('instructions.json',self.spec)
            approved=agent.run(instructions,root=self.root)
            self.assertEqual(approved['unique_artifacts'][0]['status'],'eligible_dry_run')
            policy['resources'][0]['operations']['local_inference']='unreviewed'
            self.write('configs/data_licenses.json',policy)
            denied=agent.run(instructions,execute=True,root=self.root)
            self.assertEqual(denied['unique_artifacts'][0]['status'],'blocked_license')

    def test_csv_handoff_preserves_mode_limits_and_original_file(self):
        self.fixture();path=self.root/'download-instructions.csv'
        common={k:v for k,v in self.spec.items() if k!='dataset_ids'}
        common['technical_risk_acknowledgements']=json.dumps(common['technical_risk_acknowledgements'])
        with path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=['id',*common])
            writer.writeheader();writer.writerows({'id':id,**common} for id in ['A','B'])
        parsed=agent.read_instructions(path)
        self.assertEqual(parsed['dataset_ids'],['A','B'])
        self.assertEqual(parsed['license_mode'],'lenient')
        self.assertEqual(parsed['max_total_bytes'],2048)
        with patch.object(agent.acquisition.subprocess,'run',side_effect=AssertionError('network')):
            report=agent.run(path,root=self.root)
        self.assertEqual(report['unique_artifacts'][0]['status'],'eligible_dry_run')
        run=next((self.root/'runs/downloads').iterdir())
        self.assertEqual((run/'original-instructions.csv').read_bytes(),path.read_bytes())
        path.write_text('id,id\nA,B\n')
        with self.assertRaisesRegex(ValueError,'invalid_csv_headers'):agent.read_instructions(path)

    def test_csv_with_different_licensing_between_rows_is_rejected(self):
        path=self.root/'instructions.csv';common={k:v for k,v in self.spec.items() if k!='dataset_ids'}
        common['technical_risk_acknowledgements']='[]'
        with path.open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=['id',*common]);writer.writeheader()
            writer.writerow({'id':'A',**common})
            writer.writerow({'id':'B',**common,'license_mode':'restrictive'})
        with self.assertRaisesRegex(ValueError,'inconsistent_csv_agent_settings'):agent.read_instructions(path)

    def test_full_and_review_requests_never_silently_execute_a_minimal_download(self):
        instructions=self.fixture()
        for scope in ['review_only','full']:
            self.spec.update(download_scope=scope,download_requested=scope!='review_only');self.write('instructions.json',self.spec)
            with patch.object(agent.acquisition.subprocess,'run',side_effect=AssertionError('network')):
                report=agent.run(instructions,execute=True,root=self.root)
            self.assertEqual(report['download_scope'],scope)
            self.assertEqual(report['unique_artifacts'],[])
            self.assertEqual(report['dataset_results'][0]['status'],'review_only_no_transfer' if scope=='review_only' else 'full_release_review_required')

    def test_dataset_limit_counts_the_combined_package_before_any_transfer(self):
        instructions=self.fixture()
        self.spec.update(max_bytes_per_artifact=20_000_000_000,max_bytes_per_dataset=20_000_000_000,max_total_bytes=40_000_000_000)
        self.write('instructions.json',self.spec)
        jobs=[{**self.job,'max_bytes':11_000_000_000},{**self.job,'id':'second_part','path':'part2.json','url':'https://provider.example/part2.json','max_bytes':11_000_000_000}]
        self.write('configs/research_resources.json',{'resources':jobs})
        self.write('configs/resource_scope.json',{'decisions':[{'id':j['id'],'retain':True} for j in jobs]})
        reviews=agent.read(self.root/'configs/dataset_download_reviews.json')
        for row in reviews['reviews']:row['resource_aliases']=[j['id'] for j in jobs]
        self.write('configs/dataset_download_reviews.json',reviews)
        with patch.object(agent.acquisition.subprocess,'run',side_effect=AssertionError('network')):
            report=agent.run(instructions,execute=True,root=self.root)
        self.assertEqual(report['max_bytes_per_dataset'],20_000_000_000)
        self.assertEqual(report['dataset_results'][0]['status'],'blocked_dataset_size_limit')
        self.assertEqual(report['dataset_results'][0]['manifest_budget_upper_bound'],22_000_000_000)
        self.assertEqual(report['unique_artifacts'],[])
        self.assertFalse((self.root/report['quarantine_directory']).exists())
        with self.assertRaisesRegex(ValueError,'maximum_dataset_size'):
            agent.validate_instructions({**self.spec,'max_bytes_per_dataset':20_000_000_001},self.spec['catalogue_sha256'])


if __name__=='__main__':unittest.main()
