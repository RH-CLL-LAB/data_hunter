"""Intake launch plans must not call a model without explicit execution."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('intake_agent',Path(__file__).parents[1]/'Scripts/run_dataset_intake_agent.py')
agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)

class IntakeAgentTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();(self.root/'agents').mkdir()
        (self.root/agent.ROLE).write_text('Test fixture intake role')
        self.request={'schema_version':1,'kind':'datahunter_dataset_intake','task_type':'single_source','dataset_name':'Example',
                      'purpose':'Clinical variable mapping','source_urls':['https://provider.example/release'],
                      'license_mode':'lenient','download_scope':'review_only','download_requested':False,'max_bytes_per_dataset':20_000_000_000,'public_metadata_only':True,'intake_phase':'license_first','max_metadata_bytes':5_000_000}
        self.path=self.root/'request.json';self.path.write_text(json.dumps(self.request))

    def test_invalid_sources_modes_and_missing_public_scope_are_rejected(self):
        for change in [{'license_mode':'ignore'},{'public_metadata_only':False},{'download_requested':'true'},
                       {'source_urls':['file:///private/data']},{'source_urls':['https://user:secret@example.org']},
                       {'source_urls':['https://127.0.0.1/data']},{'source_urls':['https://localhost/data']},
                       {'source_urls':['https://[::1]/data']},{'source_urls':['https://10.0.0.2/data']}]:
            with self.subTest(change=change),self.assertRaises(ValueError):agent.validate({**self.request,**change})

    def test_discovery_accepts_no_url_but_single_source_still_requires_one(self):
        request={**self.request,'task_type':'discovery','source_urls':[]}
        self.assertEqual(agent.validate(request)['source_urls'],[])
        self.assertEqual(agent.validate(request)['task_type'],'discovery')
        for changes in [{'task_type':'single_source'},{'task_type':'unknown'},
                        {'source_urls':['https://localhost/private']}]:
            with self.assertRaises(ValueError):agent.validate({**request,**changes})
        legacy={k:v for k,v in self.request.items() if k!='task_type'}
        self.assertEqual(agent.validate(legacy)['task_type'],'single_source')

    def test_new_source_intake_is_review_only_with_a_bounded_metadata_budget(self):
        for scope in ['minimal','full']:
            with self.assertRaisesRegex(ValueError,'license_first_intake'):
                agent.validate({**self.request,'download_scope':scope,'download_requested':True})
        legacy={k:v for k,v in self.request.items() if k not in {'intake_phase','max_metadata_bytes'}}
        self.assertEqual(agent.validate(legacy)['intake_phase'],'license_first')
        self.assertEqual(agent.validate(legacy)['max_metadata_bytes'],5_000_000)
        for changes in [{'max_metadata_bytes':5_000_001},{'max_metadata_bytes':True},{'intake_phase':'download'}]:
            with self.assertRaises(ValueError):agent.validate({**self.request,**changes})
        with self.assertRaisesRegex(ValueError,'maximum_dataset_size'):
            agent.validate({**self.request,'max_bytes_per_dataset':20_000_000_001})

    def test_default_plan_keeps_request_role_prompt_and_makes_no_model_call(self):
        with patch.object(agent.subprocess,'Popen',side_effect=AssertionError('model must not start')):
            job=agent.launch(self.path,root=self.root)
        self.assertEqual(job['state'],'planned');self.assertFalse(job['codex_launch_requested'])
        run=self.root/job['run'];self.assertTrue((run/'prompt.txt').is_file())
        self.assertEqual(json.loads((run/'request.json').read_text()),self.request)
        command=job['command'];self.assertIn('workspace-write',command)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',command)
        self.assertEqual(command[-1],'-')

    def test_background_requires_execute_and_preserves_strict_mode(self):
        with self.assertRaisesRegex(ValueError,'background_requires_explicit_execute'):
            agent.launch(self.path,background=True,root=self.root)
        self.request['license_mode']='restrictive';self.path.write_text(json.dumps(self.request))
        with patch.object(agent.shutil,'which',return_value=sys.executable),patch.object(agent.subprocess,'Popen',return_value=SimpleNamespace(pid=1234)) as start:
            job=agent.launch(self.path,execute=True,background=True,root=self.root)
        self.assertEqual(job['license_mode'],'restrictive')
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        self.assertIn('--worker',start.call_args.args[0])
        self.assertTrue((self.root/job['run']/'launcher.json').is_file())

    def test_missing_cli_blocks_execution(self):
        with patch.object(agent.shutil,'which',return_value=None),patch.object(agent.subprocess,'Popen',side_effect=AssertionError('must not start')):
            job=agent.launch(self.path,execute=True,root=self.root)
        self.assertEqual(job['state'],'blocked_cli_missing');self.assertFalse(job['codex_launch_requested'])

    def test_worker_records_failure_without_claiming_registration(self):
        job=agent.launch(self.path,root=self.root);run=self.root/job['run']
        with patch.object(agent.subprocess,'Popen',return_value=SimpleNamespace(pid=1234,wait=lambda **kwargs:2)):
            result=agent.worker(run,root=self.root)
        self.assertEqual(result['state'],'cli_failed');self.assertEqual(result['exit_code'],2)
        self.assertFalse(result['semantic_registration_verified'])
        with self.assertRaises(ValueError):agent.worker(self.root,root=self.root)

if __name__=='__main__':unittest.main()
