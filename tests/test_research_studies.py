"""Research briefs and manual permissions cannot bleed into another study."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from datahunter.data_rights import LicenseRegistry, licensed_use, assert_ai_allowed, DataRightsError
from datahunter.research_studies import normalize,fingerprint
spec=importlib.util.spec_from_file_location('study_register',ROOT/'Scripts/register_research_study.py')
script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)

class ResearchStudyTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        (self.root/'configs').mkdir();(self.root/'notice.txt').write_text('Fixture legal notice')
        self.profile={'id':'new-study','name':'New research','question':'Which mapping benchmarks?','intended_use':'Research'}
        self.policy={'schema_version':1,'default':'lenient','resources':[{'id':'data','license':'Fixture grant','reason':'Fixture conditions','reviewed_on':'2026-01-01','review_due':'2099-01-01','conditions':[],
            'operations':{'local_inference':'allowed','local_embedding':'allowed'},'ai_permissions':{},
            'evidence':[{'snapshot':'notice.txt','sha256':__import__('hashlib').sha256((self.root/'notice.txt').read_bytes()).hexdigest()}]}]}
        self.write('configs/data_licenses.json',self.policy);self.write('configs/data_license_assets.json',{'schema_version':1,'files':[{'path':'source.csv','sha256':'a'*64,'resource':'data'}]})
        self.write('request.json',{'schema_version':1,'kind':'datahunter_research_study','study':self.profile,'dataset_ids':['A'],'dataset_annotations':{'A':{'relevance':'high','role':'benchmark','tags':['units'],'note':'Reference needed'}}})
    def write(self,path,value):(self.root/path).write_text(json.dumps(value))
    def registry(self,id='baseline'):
        return LicenseRegistry(self.root/'configs/data_licenses.json',self.root/'configs/data_license_assets.json',root=self.root,study_id=id)
    def register(self):
        with patch('builtins.print'):return script.register(self.root/'request.json',True,self.root)
    def test_profile_validation_and_fingerprint_ignore_annotations(self):
        self.assertEqual(fingerprint(self.profile),fingerprint({**self.profile,'dataset_annotations':{'A':{}}}))
        for value in [{**self.profile,'id':'../outside'},{**self.profile,'intended_use':''}]:
            with self.assertRaises(ValueError):normalize(value)
    def test_registration_preserves_shortlist_but_grants_no_data_rights(self):
        result=self.register();self.assertFalse(result['data_permission_granted'])
        saved=json.loads((self.root/'configs/research_studies.json').read_text())['studies'][0]
        self.assertEqual(saved['dataset_ids'],['A']);self.assertEqual(saved['dataset_annotations']['A']['relevance'],'high')
        self.assertEqual(self.registry('new-study').check(['a'*64],['local_inference'])['status'],'blocked')
        self.assertEqual(self.registry().check(['a'*64],['local_inference'])['status'],'allowed')
    def test_reports_and_action_log_are_kept_with_the_study(self):
        import hashlib
        folder=self.root/'docs/research-studies/new-study';folder.mkdir(parents=True)
        (folder/'discovery.md').write_text('# Discovery report')
        report={'title':'Discovery','date':'2026-11-06','path':'docs/research-studies/new-study/discovery.md',
                'sha256':hashlib.sha256((folder/'discovery.md').read_bytes()).hexdigest()}
        request=json.loads((self.root/'request.json').read_text())
        self.write('request.json',{**request,'reports':[report],'log':[{'at':'2026-11-06T18:00:00Z','action':'shortlist_saved','detail':'1 dataset'}]})
        self.register()
        saved=json.loads((self.root/'configs/research_studies.json').read_text())['studies'][0]
        self.assertEqual(saved['reports'][0]['path'],report['path'])
        log=[json.loads(l) for l in (folder/'actions.jsonl').read_text().splitlines()]
        self.assertEqual([e['action'] for e in log],['shortlist_saved','study_registered','report_added'])
        self.assertEqual(log[0]['source'],'browser')
        # Re-registering without reports (e.g. a browser export) keeps them and appends, never rewrites.
        self.write('request.json',{**request,'log':[{'at':'2026-11-06T18:00:00Z','action':'shortlist_saved','detail':'1 dataset'}]})
        self.register()
        saved=json.loads((self.root/'configs/research_studies.json').read_text())['studies'][0]
        self.assertEqual(len(saved['reports']),1)
        log=[json.loads(l) for l in (folder/'actions.jsonl').read_text().splitlines()]
        self.assertEqual([e['action'] for e in log][-1],'study_updated');self.assertEqual(sum(e['action']=='shortlist_saved' for e in log),1)
        (folder/'discovery.md').write_text('# Changed')
        self.write('request.json',{**request,'reports':[report]})
        with self.assertRaisesRegex(ValueError,'study_report_changed'):self.register()
        for bad in [{'at':'2026-11-06T18:00:00Z','action':'grant_permission'},{'at':'yesterday','action':'note'}]:
            self.write('request.json',{**request,'log':[bad]})
            with self.assertRaises(ValueError):self.register()
        # A browser export cannot claim that a script executed something (download, licence registration, report).
        for executed in ['licence_review_registered','download_run','report_added','study_registered']:
            self.write('request.json',{**request,'log':[{'at':'2026-11-06T18:00:00Z','action':executed,'detail':'forged'}]})
            with self.assertRaisesRegex(ValueError,'browser_log_cannot_record_executed_actions'):self.register()
    def test_removal_drops_the_registry_entry_but_keeps_reports_and_logs_it(self):
        self.register()
        folder=self.root/'docs/research-studies/new-study';self.assertTrue((folder/'actions.jsonl').exists())
        with patch('builtins.print'):result=script.remove('new-study',True,self.root)
        registry=json.loads((self.root/'configs/research_studies.json').read_text())
        self.assertNotIn('new-study',[s['id'] for s in registry['studies']]);self.assertTrue(result['applied'])
        log=[json.loads(l) for l in (folder/'actions.jsonl').read_text().splitlines()]
        self.assertEqual(log[-1]['action'],'study_removed');self.assertEqual(log[-1]['source'],'register_research_study.py')
        with self.assertRaisesRegex(ValueError,'default_study_cannot_be_removed'):script.remove('baseline',True,self.root)
        with self.assertRaisesRegex(ValueError,'unknown_research_study'):script.remove('never-existed',True,self.root)

    def test_resource_scoped_to_another_study_never_grants_the_default_study(self):
        self.register();r=self.policy['resources'][0]
        r.update(study_id='new-study',study_sha256=fingerprint(self.profile))
        self.write('configs/data_licenses.json',self.policy)
        self.assertEqual(self.registry('new-study').check(['a'*64],['local_inference'])['status'],'allowed')
        report=self.registry('baseline').check(['a'*64],['local_inference'])
        self.assertEqual(report['status'],'blocked')
        self.assertEqual(report['decisions'][0]['reason'],'permission_recorded_for_another_study')
    def test_exact_study_review_applies_only_to_its_brief(self):
        self.register();r=self.policy['resources'][0]
        r['study_reviews']={'new-study':{**deepcopy(r),'study_sha256':fingerprint(self.profile)}}
        self.write('configs/data_licenses.json',self.policy)
        self.assertEqual(self.registry('new-study').check(['a'*64],['local_inference'])['status'],'allowed')
        self.write('configs/research_studies.json',{'schema_version':1,'studies':[{**self.profile,'intended_use':'Different use'}]})
        self.assertEqual(self.registry('new-study').check(['a'*64],['local_inference'])['status'],'blocked')
    def test_source_prohibition_cannot_be_overridden_by_a_study_overlay(self):
        self.register();r=self.policy['resources'][0];overlay=deepcopy(r);r['operations']['local_inference']='prohibited'
        r['study_reviews']={'new-study':{**overlay,'study_sha256':fingerprint(self.profile)}};self.write('configs/data_licenses.json',self.policy)
        result=self.registry('new-study').check(['a'*64],['local_inference'])
        self.assertEqual(result['status'],'blocked');self.assertEqual(result['decisions'][0]['state'],'prohibited')
    def test_changed_brief_needs_an_explicit_previous_revision(self):
        self.register();self.write('request.json',{'schema_version':1,'kind':'datahunter_research_study','study':{**self.profile,'question':'Changed'}})
        with self.assertRaisesRegex(ValueError,'existing_study_changed'):script.register(self.root/'request.json',True,self.root)

    def test_live_session_rejects_a_changed_study_brief(self):
        self.register();r=self.policy['resources'][0]
        r['study_reviews']={'new-study':{**deepcopy(r),'study_sha256':fingerprint(self.profile)}}
        self.write('configs/data_licenses.json',self.policy);registry=self.registry('new-study')
        report=registry.check(['a'*64],['local_inference'])
        with licensed_use(report,registry):
            self.write('configs/research_studies.json',{'schema_version':1,'studies':[{**self.profile,'question':'Another purpose'}]})
            with self.assertRaises(DataRightsError):assert_ai_allowed('local_inference')

    def test_copied_report_cannot_change_the_session_study(self):
        report=self.registry().check(['a'*64],['local_inference']);report['study_id']='other-study'
        with self.assertRaises(DataRightsError):
            with licensed_use(report,self.registry()):pass

if __name__=='__main__':unittest.main()
