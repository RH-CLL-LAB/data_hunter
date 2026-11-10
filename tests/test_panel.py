"""Activity panel model: running scripts, study milestones and what is missing; offline fixtures."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter import panel  # noqa: E402


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)


PS = """  PID ELAPSED  %CPU COMMAND
  101 01:02:03  12.5 .venv/bin/python Scripts/resolve_full_release.py --instructions studies/demo/runs/x/original-instructions.csv --execute --workers 4
  102 01:02:03   0.0 caffeinate -i .venv/bin/python Scripts/resolve_full_release.py --instructions studies/demo/runs/x/original-instructions.csv --execute
  103 03:00:00   0.0 .venv/bin/python Scripts/serve.py 8768
  104 00:00:10   0.0 grep Scripts/
  105 03:00:00   0.0 /Applications/Claude.app/Contents/Helpers/disclaimer -- .venv/bin/python Scripts/serve.py 8768
  106 00:00:05   1.0 /usr/bin/python3 -I Scripts/report_dataset_status.py --report
"""


class PanelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.study = self.root / 'demo'
        write(self.study / 'study.json', {'schema_version': 1, 'id': 'demo'})
        write(self.study / 'configs/research_studies.json', {'studies': [{'id': 'demo', 'name': 'Demo study', 'reports': [{'path': 'reports/a.md'}]}]})
        write(self.study / 'actions.jsonl', '\n'.join(json.dumps(a) for a in [{'at': '2026-01-01T00:00:00+00:00', 'action': 'study_registered'}, {'at': '2026-01-02T00:00:00+00:00', 'action': 'discovery_run'}]) + '\n')
        write(self.study / 'configs/research_resources.json', {'resources': [{'id': 'a', 'path': 'datasets/x/a.csv'}, {'id': 'b', 'path': 'datasets/x/b.csv'}]})
        write(self.study / 'configs/research_resources.lock.json', {'files': [{'id': 'a', 'path': 'datasets/x/a.csv', 'bytes': 1234, 'sha256': 'x'}]})
        write(self.study / 'docs/DATASET_STATUS.md', '# Status\n\nCounts: 3 automatic, 1 partial, 2 action needed, 1 no route.\n')

    def test_running_processes_are_parsed_without_wrappers_or_the_grep_itself(self):
        rows = panel.running_processes(PS)
        self.assertEqual([(r['pid'], r['script'], r['study'], r['kind']) for r in rows], [(101, 'resolve_full_release.py', 'demo', 'job'), (103, 'serve.py', None, 'site server'), (106, 'report_dataset_status.py', None, 'job')])
        self.assertEqual(rows[0]['elapsed'], '01:02:03')

    def test_study_summary_reports_milestones_holdings_counts_and_what_is_missing(self):
        s = panel.study_summary(self.study, running_studies={'demo'})
        self.assertEqual({m['title']: m['done'] for m in s['milestones']}, {'Registered': True, 'Discovery run': True, 'Catalogue built': False, 'Status report': True, 'Downloads started': True, 'All registered files held': False})
        self.assertEqual((s['done'], s['total'], s['registered_files'], s['held_files'], s['held_bytes'], s['reports'], s['actions'], s['running']), (4, 6, 2, 1, 1234, 1, 2, True))
        self.assertEqual(s['status_counts'], {'automatic': 3, 'partial': 1, 'action_needed': 2, 'no_route': 1})
        self.assertEqual(s['missing'], ['2 datasets need your action (registration, agreement or terms), 1 partial, 1 no route', '1 registered files not held yet'])

    def test_resolver_progress_follows_the_journal_of_an_unfinished_run(self):
        run = self.study / 'runs/full_release/20260101T000000000000Z'
        rows = [{'at': '2026-01-01T00:00:00+00:00', 'kind': 'run_started', 'details': {'dataset_ids': ['A', 'B']}},
                {'at': '2026-01-01T00:00:01+00:00', 'kind': 'dataset_resolved', 'details': {'dataset': 'A', 'outcome': 'ready', 'new_jobs': 4}}]
        write(run / 'operations.jsonl', '\n'.join(json.dumps(r) for r in rows) + '\n')
        p = panel.resolver_progress(self.study)
        self.assertEqual((p['phase'], p['percent']), ('resolving 1 of 2 releases', 50.0))
        rows += [{'at': '2026-01-01T00:00:02+00:00', 'kind': 'dataset_resolved', 'details': {'dataset': 'B', 'outcome': 'blocked_access_registration', 'new_jobs': 0}},
                 {'at': '2026-01-01T00:00:03+00:00', 'kind': 'acquired', 'details': {'id': 'a1'}}, {'at': '2026-01-01T00:00:04+00:00', 'kind': 'transfer_failed', 'details': {'id': 'a2'}}]
        write(run / 'operations.jsonl', '\n'.join(json.dumps(r) for r in rows) + '\n')
        p = panel.resolver_progress(self.study)
        self.assertEqual((p['phase'], p['percent']), ('transferring 1 of 4 files, 1 failed', 25.0))
        write(run / 'report.json', {'done': True})
        self.assertIsNone(panel.resolver_progress(self.study), 'a finished run is no longer in flight')

    def test_panel_model_lists_every_study_workspace_and_is_json_serialisable(self):
        model = panel.panel_model(self.root, ps_output=PS)
        self.assertEqual([s['id'] for s in model['studies']], ['demo'])
        self.assertEqual(len(model['processes']), 3)
        json.dumps(model)


if __name__ == '__main__':
    unittest.main()
