"""Merging one study's shareable facts into another: pre-access entries, re-bound unreviewed licence records, refused conflicts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.data_rights import LicenseRegistry  # noqa: E402
from datahunter.research_studies import read_log  # noqa: E402
spec = importlib.util.spec_from_file_location('merge_study_facts', CODE_ROOT / 'Scripts/merge_study_facts.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

NOTICE = 'data/raw/public_reference_collection/acquisitions/intake_2026-11-01/notices/provider_license.html'
OTHER_NOTICE = 'data/raw/public_reference_collection/acquisitions/intake_2026-11-01/notices/provider_project.html'


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1) if not isinstance(value, str) else value)


def dataset(ident, held=False, **extra):
    entry = {'id': ident, 'name': ident + ' name', 'scope': 'what it is', 'source_urls': ['https://example.org/' + ident], 'uses': [{'study': 'paper_a', 'relation': 'evaluation', 'scope': 's'}],
             'paths': [], 'application_gap': 'gap', 'audit_state': 'pre_access_registered', 'verified_artifacts': [], 'completeness': 'not_obtained', 'file_integrity': 'not_checked',
             'full_paper_reproduction_ready': False, 'missing_components': ['Open release not yet acquired.'], 'next_action': 'Ask for the release.',
             'access_route': 'public_no_registration', 'registration_url': None, 'priority': 'P2'}
    if held:
        entry.update({'paths': ['data/acquisitions/datasets/' + ident.lower() + '/'], 'audit_state': 'acquired_locked_verified', 'verified_artifacts': ['data/acquisitions/datasets/x.csv'],
                      'completeness': 'complete_public_release', 'file_integrity': 'provider_checksums_verified', 'missing_components': [],
                      'next_action': 'Complete release held (3 files).', 'data_caveats': ['Identity: local hash only.']})
    entry.update(extra)
    return entry


def licence(ident, evidence, **extra):
    record = {'id': ident, 'name': ident, 'license': 'CC-BY-4.0', 'scope': 'data', 'reviewed_on': '2026-11-01', 'review_due': '2099-01-01', 'study_id': 'source-study',
              'study_sha256': 'f' * 64, 'reason': 'license_conditions_recorded', 'conditions': ['Attribution.'], 'sources': ['https://example.org/licence'], 'catalog_ids': ['HELD'],
              'operations': {'local_inference': 'allowed', 'local_embedding': 'allowed', 'remote_inference': 'unreviewed', 'remote_embedding': 'unreviewed', 'training': 'prohibited', 'redistribution': 'unreviewed'},
              'ai_permissions': {'local_inference': 'allowed', 'training': 'prohibited'}, 'evidence': evidence,
              'manual_review': {'reviewer': 'someone', 'purpose': 'source study purpose', 'study_id': 'source-study'},
              'study_reviews': {'third-study': {'operations': {'local_inference': 'allowed'}}}}
    record.update(extra)
    return record


def workspace(root, study_id, *, datasets=(), papers=(), licences=(), notices=None, selection=None, validation=None):
    write(root / 'study.json', {'schema_version': 1, 'id': study_id})
    profile = {'id': study_id, 'name': study_id, 'question': 'q', 'intended_use': 'u', 'population': '', 'targets': '', 'inclusion': '', 'exclusion': ''}
    write(root / 'configs/research_studies.json', {'schema_version': 1, 'default_study': study_id, 'studies': [
        {**profile, 'dataset_ids': ['OWN'], 'dataset_annotations': {'OWN': {'relevance': 'high'}}, 'study_annotations': {}}]})
    write(root / 'configs/dataset_study_usage.json', {'schema_version': 1, 'reviewed_on': '2026-11-01', 'scope': 'fixture', 'studies': list(papers), 'datasets': list(datasets)})
    own_screen = {'id': 'own_screen', 'dataset_ids': ['OWN'], 'status': 'public_terms_screened_conditional', 'sources': ['https://example.org/own'], 'evidence': [], 'rights_granted': False}
    write(root / 'configs/dataset_selection.json', {'schema_version': 1, 'default': {'action_priority': 'P3 / hold'}, 'priority_groups': [], 'relevance_overrides': {},
                                                     'preaccess_license_policy': {'reviews': [own_screen], 'coverage': {'required_registration_ids': [], 'deferred_registration_ids': []}}, **(selection or {})})
    write(root / 'configs/dataset_validation.json', {'schema_version': 1, 'groups': {}, 'families': {}, 'shared_family_notes': {}, 'review_notes': {}, **(validation or {})})
    write(root / 'configs/data_licenses.json', {'schema_version': 1, 'default': 'lenient', 'resources': list(licences)})
    write(root / 'configs/data_license_assets.json', {'schema_version': 1, 'files': []})
    for rel, text in (notices or {}).items():
        write(root / rel, text)
    return root


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class MergeStudyFactsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.paper = {'id': 'paper_a', 'title': 'Paper A', 'document_type': 'research_article', 'urls': ['https://doi.org/10.1/a']}
        self.notices = {NOTICE: '<html>CC BY 4.0</html>', OTHER_NOTICE: '<html>project page</html>'}
        self.record = licence('provider_cc_by', [{'snapshot': NOTICE, 'sha256': sha(self.notices[NOTICE]), 'public_notice': True}])
        self.screen = {'id': 'provider_terms', 'dataset_ids': ['HELD', 'PRE', 'ABSENT'], 'scope': 'provider terms', 'status': 'public_notice_only_agreement_pending', 'summary': 's', 'finding': 'f', 'next': 'n',
                       'sources': ['https://example.org/licence'], 'evidence': [{'path': NOTICE, 'sha256': sha(self.notices[NOTICE])}], 'rights_granted': True, 'public_dataset_or_route_agreement_reviewed': True}
        self.source = workspace(base / 'source-study', 'source-study', datasets=[dataset('HELD', held=True), dataset('PRE', access_route='registration_credentialed_access'), dataset('DEF', access_route='registration_data_use_agreement')],
                                papers=[self.paper], licences=[self.record], notices=self.notices,
                                selection={'priority_groups': [{'priority': 'P1', 'ids': ['HELD'], 'action': 'start'}, {'priority': 'P2', 'ids': ['PRE'], 'action': 'hold'}],
                                           'relevance_overrides': {'HELD': {'workflow_relevance': 'high', 'reason': 'r'}},
                                           'preaccess_license_policy': {'reviews': [self.screen], 'coverage': {'required_registration_ids': ['PRE'], 'deferred_registration_ids': ['DEF']}}},
                                validation={'groups': {'clinical_sources': ['HELD'], 'pending_access': ['PRE']}, 'families': {'fam': ['HELD', 'PRE']},
                                            'shared_family_notes': {'fam': 'same cohort'}, 'review_notes': {'HELD': 'check the card'}})
        self.target = workspace(base / 'target-study', 'target-study')

    def merge(self, apply):
        return module.merge(self.source, self.target, apply=apply)

    def test_dry_run_reports_and_changes_nothing(self):
        before = snapshot(self.target)
        with patch('builtins.print'):
            report = module.main(['--source', str(self.source), '--target', str(self.target)])
        self.assertFalse(report['applied'])
        self.assertEqual(report['counts']['datasets'], {'add': 3, 'present': 0, 'conflict': 0})
        self.assertEqual(report['counts']['preaccess'], {'add': 1, 'present': 0, 'skipped': 0, 'blocked': 0, 'required_registration_ids': 1, 'deferred_registration_ids': 1})
        self.assertEqual(report['counts']['files']['copy'], 2)
        self.assertEqual(report['counts']['licences']['add'], 1)
        after = snapshot(self.target)
        self.assertEqual({k: v for k, v in after.items() if not k.startswith('runs/')}, before, 'a dry run leaves the registries, files and logs alone')
        self.assertEqual(sorted(k.split('/')[-1] for k in after if k.startswith('runs/merge_study_facts/')), ['report.json', 'request.json'])
        self.assertFalse((self.target / 'actions.jsonl').exists())
        self.assertFalse((self.source / 'actions.jsonl').exists())
        self.assertIn('not merged', '\n'.join(module.summary_lines(report)))

    def test_entry_is_added_as_pre_access_with_the_held_state_reset(self):
        report = self.merge(apply=True)
        self.assertTrue(report['applied'])
        usage = json.loads((self.target / 'configs/dataset_study_usage.json').read_text())
        held = next(d for d in usage['datasets'] if d['id'] == 'HELD')
        self.assertEqual((held['paths'], held['audit_state'], held['verified_artifacts'], held['completeness'], held['file_integrity'], held['full_paper_reproduction_ready']),
                         ([], 'pre_access_registered', [], 'not_obtained', 'not_checked', False))
        self.assertNotIn('data_caveats', held)
        self.assertIn('Not held by this study', held['next_action'])
        self.assertIn('--reuse-from-study source-study', held['next_action'])
        self.assertEqual((held['name'], held['source_urls'], held['uses'], held['access_route'], held['priority']), ('HELD name', ['https://example.org/HELD'], [{'study': 'paper_a', 'relation': 'evaluation', 'scope': 's'}], 'public_no_registration', 'P2'))
        pre = next(d for d in usage['datasets'] if d['id'] == 'PRE')
        self.assertEqual((pre['next_action'], pre['missing_components']), ('Ask for the release.', ['Open release not yet acquired.']), 'a source entry that was never held keeps its own route text')
        self.assertEqual(usage['studies'], [self.paper])
        selection = json.loads((self.target / 'configs/dataset_selection.json').read_text())
        self.assertEqual(selection['priority_groups'], [{'priority': 'P1', 'ids': ['HELD'], 'action': 'start'}, {'priority': 'P2', 'ids': ['PRE'], 'action': 'hold'}])
        self.assertEqual(selection['relevance_overrides'], {'HELD': {'workflow_relevance': 'high', 'reason': 'r'}})
        screens = selection['preaccess_license_policy']['reviews']
        self.assertEqual([(r['id'], r['dataset_ids'], r['rights_granted']) for r in screens], [('own_screen', ['OWN'], False), ('provider_terms', ['HELD', 'PRE'], False)],
                         'the target keeps its own screen; the copied screen covers only catalogue datasets and never grants rights')
        self.assertEqual((screens[1]['status'], screens[1]['evidence'], screens[1]['merged_from']['study']), ('public_notice_only_agreement_pending', self.screen['evidence'], 'source-study'))
        self.assertEqual(selection['preaccess_license_policy']['coverage'], {'required_registration_ids': ['PRE'], 'deferred_registration_ids': ['DEF']},
                         'registration-route entries join the census by the priority they land on; the unplaced one takes the default P3 / hold')
        validation = json.loads((self.target / 'configs/dataset_validation.json').read_text())
        self.assertEqual((validation['groups'], validation['families'], validation['shared_family_notes'], validation['review_notes']),
                         ({'clinical_sources': ['HELD'], 'pending_access': ['PRE']}, {'fam': ['HELD', 'PRE']}, {'fam': 'same cohort'}, {'HELD': 'check the card'}))
        for rel, text in self.notices.items():
            self.assertEqual((self.target / rel).read_text(), text)
        studies = json.loads((self.target / 'configs/research_studies.json').read_text())['studies'][0]
        self.assertEqual((studies['dataset_ids'], studies['dataset_annotations']), (['OWN'], {'OWN': {'relevance': 'high'}}), 'shortlist and annotations are never merged')
        self.assertEqual(json.loads((self.target / 'configs/data_license_assets.json').read_text())['files'], [], 'licence assets bind held files and stay behind')
        run = self.target / report['run']
        self.assertEqual(sorted(p.name for p in run.iterdir()), ['before-data_licenses.json', 'before-dataset_selection.json', 'before-dataset_study_usage.json', 'before-dataset_validation.json', 'merge_study_facts.py', 'report.json', 'request.json'])
        self.assertEqual(json.loads((run / 'request.json').read_text())['source']['study'], 'source-study')
        target_log, source_log = read_log(self.target, 'target-study'), read_log(self.source, 'source-study')
        self.assertEqual((len(target_log), target_log[0]['action'], target_log[0]['run']), (1, 'note', report['run']))
        self.assertIn('from study source-study', target_log[0]['detail'])
        self.assertIn('3 catalogue entries added as pre-access', target_log[0]['detail'])
        self.assertIn('1 terms screens (no rights granted)', target_log[0]['detail'])
        self.assertEqual((len(source_log), source_log[0]['action']), (1, 'note'))
        self.assertIn('into study target-study', source_log[0]['detail'])
        self.assertIn(report['run'], source_log[0]['run'])
        self.assertEqual(report['log_events_added'], 2)
        # Running again finds everything present and adds nothing.
        again = self.merge(apply=True)
        self.assertEqual((again['counts']['datasets'], again['counts']['licences']['present'], again['counts']['files']['present']), ({'add': 0, 'present': 3, 'conflict': 0}, 1, 2))
        self.assertEqual(again['counts']['preaccess'], {'add': 0, 'present': 1, 'skipped': 0, 'blocked': 0, 'required_registration_ids': 0, 'deferred_registration_ids': 0})
        self.assertEqual(len(json.loads((self.target / 'configs/dataset_study_usage.json').read_text())['datasets']), 3)
        self.assertEqual(json.loads((self.target / 'configs/dataset_selection.json').read_text()), selection, 'a second merge changes nothing')

    def test_existing_entry_is_untouched_and_differing_facts_are_a_conflict(self):
        mine = dataset('HELD', held=True, source_urls=['https://example.org/other-release'], paths=['data/acquisitions/datasets/mine/'])
        self.target = workspace(self.target, 'target-study', datasets=[mine], papers=[{**self.paper, 'title': 'Paper A (preprint)'}],
                                selection={'priority_groups': [{'priority': 'P3 / hold', 'ids': ['HELD'], 'action': 'wait'}]}, validation={'groups': {'excluded': ['HELD']}})
        before = json.loads((self.target / 'configs/dataset_study_usage.json').read_text())
        report = self.merge(apply=True)
        after = json.loads((self.target / 'configs/dataset_study_usage.json').read_text())
        self.assertEqual(after['datasets'][0], before['datasets'][0], 'an existing entry is never overwritten')
        self.assertEqual(after['studies'], before['studies'])
        self.assertEqual([d['id'] for d in after['datasets']], ['HELD', 'PRE', 'DEF'])
        held = next(d for d in report['datasets'] if d['id'] == 'HELD')
        self.assertEqual((held['state'], held['differs_in']), ('conflict', ['source_urls']), 'held-state differences are not conflicts, differing facts are')
        self.assertEqual(next(p for p in report['papers'] if p['id'] == 'paper_a')['differs_in'], ['title'])
        selection = json.loads((self.target / 'configs/dataset_selection.json').read_text())
        self.assertEqual(selection['priority_groups'], [{'priority': 'P3 / hold', 'ids': ['HELD'], 'action': 'wait'}, {'priority': 'P2', 'ids': ['PRE'], 'action': 'hold'}], 'a target placement wins')
        self.assertEqual(json.loads((self.target / 'configs/dataset_validation.json').read_text())['groups'], {'excluded': ['HELD'], 'pending_access': ['PRE']})
        self.assertIn('HELD: differs in source_urls', '\n'.join(module.summary_lines(report)))

    def test_licence_allowance_becomes_unreviewed_and_the_record_is_rebound(self):
        report = self.merge(apply=True)
        policy = json.loads((self.target / 'configs/data_licenses.json').read_text())
        self.assertEqual([r['id'] for r in policy['resources']], ['provider_cc_by'])
        record = policy['resources'][0]
        self.assertEqual(record['operations'], {'local_inference': 'unreviewed', 'local_embedding': 'unreviewed', 'remote_inference': 'unreviewed', 'remote_embedding': 'unreviewed',
                                                'training': 'prohibited', 'redistribution': 'unreviewed'}, 'an allowance never crosses; a prohibition stays')
        self.assertEqual(record['ai_permissions'], {'training': 'prohibited'})
        self.assertNotIn('manual_review', record)
        self.assertNotIn('study_reviews', record)
        self.assertEqual((record['study_id'], record['catalog_ids'], record['reason']), ('target-study', ['HELD'], 'copied_from_another_study_review_required'))
        self.assertEqual((record['merged_from']['study'], record['merged_from']['reason'], record['merged_from']['run']), ('source-study', 'license_conditions_recorded', report['run']))
        self.assertEqual(record['evidence'], self.record['evidence'])
        self.assertEqual(hashlib.sha256((self.target / NOTICE).read_bytes()).hexdigest(), self.record['evidence'][0]['sha256'])
        write(self.target / 'configs/data_license_assets.json', {'schema_version': 1, 'files': [{'path': 'data/acquisitions/x.csv', 'sha256': 'a' * 64, 'resource': 'provider_cc_by'}]})
        registry = LicenseRegistry(self.target / 'configs/data_licenses.json', self.target / 'configs/data_license_assets.json', root=self.target, mode='lenient', study_id='target-study')
        decisions = registry.check(['a' * 64], ['local_inference', 'training'])['decisions']
        self.assertEqual({(d['operation'], d['state'], d['reason']) for d in decisions},
                         {('local_inference', 'unreviewed', 'copied_from_another_study_review_required'), ('training', 'prohibited', 'explicit_ai_prohibition')})

    def test_conflicting_notice_hash_is_refused_and_blocks_the_record(self):
        write(self.target / NOTICE, '<html>a different retained page</html>')
        report = self.merge(apply=True)
        self.assertEqual((self.target / NOTICE).read_text(), '<html>a different retained page</html>', 'the target’s bytes are never overwritten')
        self.assertEqual((self.target / OTHER_NOTICE).read_text(), self.notices[OTHER_NOTICE])
        conflict = next(f for f in report['files'] if f['path'] == NOTICE)
        self.assertEqual((conflict['state'], conflict['reason'], conflict['kind']), ('conflict', 'target_holds_other_bytes', 'notice'))
        blocked = next(r for r in report['licences'] if r['id'] == 'provider_cc_by')
        self.assertEqual((blocked['state'], blocked['reasons']), ('blocked', [NOTICE + ': evidence_conflicts_with_target_file']))
        self.assertEqual(json.loads((self.target / 'configs/data_licenses.json').read_text())['resources'], [])
        self.assertEqual(report['counts']['files'], {'copy': 0, 'copied': 1, 'present': 0, 'conflict': 1, 'missing': 0, 'held_file': 0})
        screen = next(s for s in report['preaccess']['screens'] if s['id'] == 'provider_terms')
        self.assertEqual((screen['state'], screen['reasons']), ('blocked', [NOTICE + ': evidence_conflicts_with_target_file']), 'a screen whose evidence conflicts stays behind too')
        self.assertEqual([r['id'] for r in json.loads((self.target / 'configs/dataset_selection.json').read_text())['preaccess_license_policy']['reviews']], ['own_screen'])
        self.assertIn('evidence_conflicts_with_target_file', '\n'.join(module.summary_lines(report)))

    def test_changed_source_evidence_and_held_file_evidence_block_the_record(self):
        write(self.source / NOTICE, '<html>edited after review</html>')
        held = licence('provider_in_holdings', [{'snapshot': 'data/acquisitions/datasets/held/LICENSE.txt', 'sha256': 'b' * 64}])
        write(self.source / 'data/acquisitions/datasets/held/LICENSE.txt', 'licence inside a held release')
        policy = json.loads((self.source / 'configs/data_licenses.json').read_text()); policy['resources'].append(held)
        write(self.source / 'configs/data_licenses.json', policy)
        report = self.merge(apply=True)
        states = {r['id']: r for r in report['licences']}
        self.assertEqual(states['provider_cc_by']['reasons'], [NOTICE + ': evidence_missing_or_changed_in_source'])
        self.assertEqual(states['provider_in_holdings']['reasons'], ['data/acquisitions/datasets/held/LICENSE.txt: evidence_is_a_held_file_acquire_or_reuse_it_first'])
        self.assertFalse((self.target / 'data/acquisitions').exists())
        self.assertEqual(json.loads((self.target / 'configs/data_licenses.json').read_text())['resources'], [])

    def test_preview_plan_writes_nothing_and_reports_counts_and_conflicts(self):
        before = snapshot(self.target), snapshot(self.source)
        plan = module.plan_json(str(self.source), str(self.target))
        self.assertEqual((snapshot(self.target), snapshot(self.source)), before, 'a preview leaves both workspaces untouched, run folder included')
        self.assertEqual((plan['source'], plan['target'], plan['data_permission_granted']), ('source-study', 'target-study', False))
        self.assertEqual(plan['counts']['datasets']['add'], 3)
        self.assertTrue(plan['summary'][0].startswith('Dry run'))
        self.assertEqual(plan['conflicts'], [])
        with self.assertRaisesRegex(ValueError, 'apply_needs_a_recorded_run'):
            module.merge(self.source, self.target, apply=True, record=False)

    def test_site_request_is_validated_before_it_is_merged(self):
        import datahunter.workspace as workspace
        request = {'schema_version': 1, 'kind': module.REQUEST_KIND, 'source': 'source-study', 'target': 'target-study', 'reviewed': True}
        with patch.object(workspace, 'STUDIES', self.target.parent):
            for broken, error in [({'kind': 'datahunter_quarantine_release'}, 'unsupported_study_facts_merge_request'), ({'reviewed': False}, 'reviewer_confirmation_required'),
                                  ({'source': '../x'}, 'invalid_merge_source'), ({'target': 'source-study'}, 'source_and_target_are_the_same_workspace')]:
                with self.subTest(broken=broken), self.assertRaisesRegex(ValueError, error):
                    module.validate_request({**request, **broken})
            with self.assertRaisesRegex(ValueError, 'merge_request_target_is_not_this_workspace'):
                module.validate_request(request, expected_target=str(self.source))
            source, target = module.validate_request(request, expected_target=str(self.target))
            self.assertEqual((source, target), (self.source, self.target))
            report = module.merge(source, target, apply=True, site_request=request)
        self.assertTrue(report['applied'])
        self.assertEqual(json.loads((self.target / report['run'] / 'site-request.json').read_text()), request, 'the saved request is kept with the run')
        self.assertEqual(report['counts']['datasets']['add'], 3)

    def test_command_line_runs_a_saved_request_as_a_dry_run_unless_applied(self):
        import datahunter.workspace as workspace
        request_path = self.target.parent / 'datahunter-merge-source-study-into-target-study.json'
        write(request_path, {'schema_version': 1, 'kind': module.REQUEST_KIND, 'source': 'source-study', 'target': 'target-study', 'reviewed': True})
        before = snapshot(self.target)
        with patch.object(workspace, 'STUDIES', self.target.parent), patch('builtins.print'):
            report = module.main(['--request', str(request_path), '--target', str(self.target)])
        self.assertFalse(report['applied'])
        self.assertEqual({k: v for k, v in snapshot(self.target).items() if 'runs/merge_study_facts' not in k}, before, 'a dry run writes only its run folder')

    def test_a_screen_whose_id_the_target_uses_covers_its_other_datasets_under_a_derived_id(self):
        selection = json.loads((self.target / 'configs/dataset_selection.json').read_text())
        selection['preaccess_license_policy']['reviews'][0]['id'] = 'provider_terms'  # the target screened OWN under the same screen id
        write(self.target / 'configs/dataset_selection.json', selection)
        report = self.merge(apply=True)
        screen = next(s for s in report['preaccess']['screens'] if s['state'] == 'add')
        self.assertEqual((screen['id'], screen['renamed_from'], screen['review']['dataset_ids']), ('provider_terms__source_study', 'provider_terms', ['HELD', 'PRE']))
        reviews = json.loads((self.target / 'configs/dataset_selection.json').read_text())['preaccess_license_policy']['reviews']
        self.assertEqual([(r['id'], r['dataset_ids']) for r in reviews], [('provider_terms', ['OWN']), ('provider_terms__source_study', ['HELD', 'PRE'])], 'the target screen is untouched; its other datasets get the source reading')
        self.assertEqual(reviews[1]['merged_from']['screen'], 'provider_terms')
        again = self.merge(apply=True)
        self.assertEqual({s['id']: s['state'] for s in again['preaccess']['screens']}, {'provider_terms': 'present'}, 'a second merge adds nothing')

    def test_workspaces_must_be_distinct_registered_studies(self):
        with self.assertRaises(SystemExit):
            module.merge(self.source, self.source)
        stray = self.target.parent / 'stray'
        stray.mkdir()
        with self.assertRaises(SystemExit):
            module.merge(self.source, stray)
        write(self.target / 'configs/research_studies.json', {'schema_version': 1, 'default_study': 'target-study', 'studies': []})
        with self.assertRaises(SystemExit):
            module.merge(self.source, self.target)


if __name__ == '__main__':
    unittest.main()
