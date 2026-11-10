"""The data-structure report profiles the held files deterministically and tells what is known before access; offline fixtures."""
from datetime import date
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

CODE_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('report_dataset_structure', CODE_ROOT / 'Scripts/report_dataset_structure.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(json.dumps(value, indent=1) if not isinstance(value, str) else value)


CSV = ('subject_id,hadm_id,admittime,admission_type,los,died\n'
       '1,101,2150-01-01 10:00:00,URGENT,3.5,false\n'
       '1,102,2150-02-01 08:30:00,ELECTIVE,,true\n'
       '2,103,2151-03-04 12:00:00,URGENT,1.25,false\n'
       '3,104,2149-12-31 23:59:00,EW EMER.,10,false\n')
ARFF = ("% comment\n@relation credit\n@attribute 'checking_status' { '<0', '0<=X<200', 'no checking'}\n@attribute 'duration' real\n"
        "@attribute class {good, bad}\n@data\n'<0',6,good\n'no checking',?,bad\n'0<=X<200',24,good\n")


class StructureReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        write(self.root / 'study.json', {'schema_version': 1, 'id': 'demo-study'})
        write(self.root / 'configs/research_studies.json', {'schema_version': 1, 'default_study': 'demo-study', 'studies': [
            {'id': 'demo-study', 'name': 'Demo study', 'question': 'q', 'intended_use': 'u', 'population': '', 'targets': '', 'inclusion': '', 'exclusion': '',
             'dataset_ids': [], 'dataset_annotations': {}, 'study_annotations': {}}]})
        folder = self.root / 'data/acquisitions/datasets/demo'
        write(folder / 'hosp/admissions.csv.gz', gzip.compress(CSV.encode()))
        write(folder / 'credit.arff', ARFF)
        folder.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(folder / 'demo.sqlite') as connection:
            connection.execute('CREATE TABLE patient (uniquepid TEXT, age INTEGER, weight REAL)')
            connection.executemany('INSERT INTO patient VALUES (?, ?, ?)', [('p1', 70, 80.5), ('p2', 65, None), ('p3', 91, 60.0)])
            connection.execute('CREATE TABLE hospital (hospitalid INTEGER, region TEXT)')
            connection.execute("INSERT INTO hospital VALUES (1, 'West')")
        with zipfile.ZipFile(folder / 'bundle.zip', 'w') as archive:
            archive.writestr('tables/patients.csv', 'Id,BIRTHDATE,GENDER\na,2000-01-02,F\nb,1990-05-06,M\n')
            archive.writestr('README.md', '# readme\n')
            archive.writestr('notes.txt', 'x\n')
        write(folder / 'events.ndjson.gz', gzip.compress(b'{"resourceType": "Patient", "id": "p1", "name": {"family": "A"}, "active": true}\n{"resourceType": "Patient", "id": "p2", "name": {"family": "B"}, "active": false}\n'))
        write(folder / 'items.json', [{'code': 'A1', 'value': 1}, {'code': 'B2', 'value': 2}])
        write(folder / 'code/run.py', 'print(1)\n')
        write(folder / 'wide.csv', 'id,label\n1,x\n2,y\n'.encode('utf-16'))  # byte-order mark, decoded as UTF-16
        write(folder / 'blob.csv', b'\x00\x01binary\x00,junk\n')
        write(folder / 'results.parquet', b'PAR1')
        self.artifacts = [{'path': str(p.relative_to(self.root)), 'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in sorted(folder.rglob('*')) if p.is_file()]
        notice = self.root / 'data/raw/notices/gated_project.html'
        write(notice, '<html>project page</html>')
        self.datasets = [
            {'id': 'HELD_DEMO', 'name': 'Held demo release', 'category': 'clinical_sources', 'access_route': 'public_no_registration', 'registration_url': None,
             'scope': 'Demo subset of the gated corpus.', 'source_urls': ['https://provider.example/demo/'], 'completeness': 'complete_public_release',
             'obtained_artifacts': self.artifacts, 'source_locators': ['data/acquisitions/datasets/demo/']},
            {'id': 'GATED', 'name': 'Gated full corpus', 'category': 'clinical_sources', 'access_route': 'registration_credentialed_access',
             'registration_url': 'https://provider.example/credentialing/', 'scope': 'Full de-identified EHR; hosp and icu modules.',
             'source_urls': ['https://provider.example/full/'], 'completeness': 'not_obtained', 'obtained_artifacts': [], 'source_locators': [],
             'preaccess_screen': {'status': 'public_notice_only_agreement_pending', 'summary': 'Public licence text; access after a signed DUA.',
                                  'sources': ['https://provider.example/full/view-license/'], 'evidence': [{'path': 'data/raw/notices/gated_project.html'}]}},
            {'id': 'OPEN_PENDING', 'name': 'Open release not acquired', 'category': 'tabular_controls', 'access_route': 'public_no_registration', 'registration_url': None,
             'scope': 'Open benchmark.', 'source_urls': [], 'completeness': 'not_obtained', 'obtained_artifacts': [], 'source_locators': []},
        ]
        selection = self.root / 'data/interim/dataset_selection/20260101T000000000000Z/selection.json'
        write(selection, {'datasets': self.datasets})
        write(self.root / 'data/interim/dataset_selection/latest.json', {'selection': str(selection.relative_to(self.root)), 'selection_sha256': hashlib.sha256(selection.read_bytes()).hexdigest(),
                                                                          'summary': {'rechecked_unique_artifacts': len(self.artifacts)}})
        write(self.root / 'configs/dataset_validation.json', {'families': {'demo_family': ['GATED', 'HELD_DEMO']}, 'limits': {'max_member_bytes_to_parse': 1024 * 1024}})
        write(self.root / 'configs/research_resources.lock.json', {'schema_version': 1, 'files': [{'id': f'f{i}', 'path': a['path'], 'sha256': a['sha256'], 'bytes': a['bytes']} for i, a in enumerate(self.artifacts)]})
        write(self.root / 'configs/data_licenses.json', {'schema_version': 1, 'default': 'lenient', 'resources': [
            {'id': 'gated_terms', 'catalog_ids': ['GATED'], 'sources': ['https://provider.example/full/view-license/'], 'evidence': [{'snapshot': 'data/raw/notices/gated_project.html', 'sha256': 'x'}],
             'operations': {'local_inference': 'unreviewed', 'local_embedding': 'unreviewed'}}]})

    def test_every_tabular_format_is_profiled_and_the_rest_is_counted(self):
        data = module.collect(self.root)
        held = next(d for d in data['datasets'] if d['id'] == 'HELD_DEMO')
        tables = {(a['path'].split('/')[-1], t.get('member')): t for a in held['artifacts'] for t in a['tables']}
        self.assertEqual(sorted(tables), [('admissions.csv.gz', None), ('bundle.zip', 'tables/patients.csv'), ('credit.arff', None), ('demo.sqlite', 'hospital'),
                                          ('demo.sqlite', 'patient'), ('events.ndjson.gz', None), ('items.json', None), ('wide.csv', None)])
        self.assertEqual(held['tables'], 8)
        self.assertEqual([c['name'] for c in tables[('wide.csv', None)]['columns']], ['id', 'label'], 'a UTF-16 table is decoded by its byte-order mark')
        self.assertEqual(held['others'], {'.md': 1, '.py': 1, '.txt': 1})
        self.assertEqual([(s['path'].split('/')[-1], s['reason']) for s in held['skipped']], [('blob.csv', 'binary_content_not_a_text_table'), ('results.parquet', 'format_adapter_missing')])
        self.assertEqual(data['datasets'][0]['family_held'], [])
        gated = next(d for d in data['datasets'] if d['id'] == 'GATED')
        self.assertEqual(gated['family_held'], ['HELD_DEMO'])
        self.assertEqual(gated['documentation'], ['data/raw/notices/gated_project.html'])
        self.assertEqual(gated['terms_sources'], ['https://provider.example/full/view-license/'])

    def test_column_types_statistics_and_labels_are_inferred_without_copying_cells(self):
        data = module.collect(self.root)
        held = next(d for d in data['datasets'] if d['id'] == 'HELD_DEMO')
        table = next(t for a in held['artifacts'] for t in a['tables'] if a['path'].endswith('admissions.csv.gz'))
        self.assertEqual((table['rows'], table['profiled_rows'], table['format']), (4, 4, 'delimited text'))
        columns = {c['name']: c for c in table['columns']}
        self.assertEqual(columns['subject_id']['type'], 'integer')
        self.assertFalse(columns['subject_id']['key_like'])
        self.assertTrue(columns['hadm_id']['key_like'], 'a column that never repeats is a candidate key')
        self.assertEqual(columns['admittime']['type'], 'datetime')
        self.assertEqual(columns['admittime']['time_range'], ['2149-12-31 23:59:00', '2151-03-04 12:00:00'])
        los = columns['los']
        self.assertEqual((los['type'], los['values'], los['missing'], los['distinct']), ('number', 3, 1, 3))
        self.assertEqual((los['numeric']['min'], los['numeric']['max'], los['numeric']['median_is_estimate']), (1.25, 10.0, False))
        self.assertAlmostEqual(los['numeric']['mean'], (3.5 + 1.25 + 10) / 3)
        self.assertEqual(columns['admission_type']['labels'], [{'value': 'URGENT', 'count': 2}, {'value': 'ELECTIVE', 'count': 1}, {'value': 'EW EMER.', 'count': 1}])
        self.assertEqual(columns['died']['type'], 'boolean')
        self.assertEqual(columns['died']['labels'], [{'value': 'false', 'count': 3}, {'value': 'true', 'count': 1}])
        arff = next(t for a in held['artifacts'] for t in a['tables'] if a['path'].endswith('credit.arff'))
        declared = {c['name']: (c['declared_type'], c['type'], c['missing']) for c in arff['columns']}
        self.assertEqual(declared, {'checking_status': ('nominal (3 levels)', 'text', 0), 'duration': ('number', 'integer', 1), 'class': ('nominal (2 levels)', 'text', 0)})
        self.assertEqual((arff['relation'], arff['rows']), ('credit', 3))
        sqlite_tables = {t['member']: t for a in held['artifacts'] for t in a['tables'] if a['path'].endswith('demo.sqlite')}
        patient = {c['name']: c for c in sqlite_tables['patient']['columns']}
        self.assertEqual((sqlite_tables['patient']['rows'], patient['age']['declared_type'], patient['age']['type'], patient['weight']['missing']), (3, 'integer', 'integer', 1))
        self.assertEqual(patient['age']['numeric']['max'], 91.0)
        ndjson = next(t for a in held['artifacts'] for t in a['tables'] if a['path'].endswith('events.ndjson.gz'))
        self.assertEqual(ndjson['resource_types'], {'Patient': 2})
        self.assertEqual({c['name']: c['type'] for c in ndjson['columns']}, {'resourceType': 'text', 'id': 'text', 'name': 'object', 'active': 'boolean'})
        member = next(t for a in held['artifacts'] for t in a['tables'] if a['path'].endswith('bundle.zip'))
        self.assertEqual({c['name']: c['type'] for c in member['columns']}, {'Id': 'text', 'BIRTHDATE': 'date', 'GENDER': 'text'})

    def test_document_profiles_held_datasets_and_tells_what_is_known_before_access(self):
        doc, data = module.write_document(self.root)
        text = doc.read_text()
        self.assertIn('## Held datasets', text)
        self.assertIn('### HELD_DEMO — Held demo release', text)
        self.assertIn('| `hadm_id` | integer | 4 | 0.0% | 4 (unique) |', text)
        self.assertIn('labels: URGENT (2), ELECTIVE (1), EW EMER. (1)', text)
        self.assertIn('Not profiled: 1 file(s): binary content not a text table; 1 file(s): format adapter missing.', text)
        headings = [l for l in text.splitlines() if l.startswith('#### ')]
        self.assertEqual(len(headings), len(set(headings)), 'every table heading is unique on the page')
        self.assertTrue(headings[0].startswith('#### HELD_DEMO · table 1 · `'), headings[0])
        self.assertNotIn('\x00', text)
        self.assertIn('## Before you apply: what is known about the datasets that are not held', text)
        gated = text[text.index('### GATED'):text.index('### OPEN_PENDING')]
        self.assertIn('registration credentialed access — [register or request](https://provider.example/credentialing/)', gated)
        self.assertIn('**Same-family releases held in this study:** [HELD_DEMO](#held_demo) (8 tables)', gated)
        self.assertIn('[gated_project.html](<../data/raw/notices/gated_project.html>)', gated)
        self.assertIn('[provider 1](https://provider.example/full/)', gated)
        self.assertIn('Unknown until access', gated)
        self.assertIn('none registered', text[text.index('### OPEN_PENDING'):])
        self.assertIn('grants no data or AI-use permission', text)
        self.assertNotIn('older than the holdings', text, 'lock and receipt agree')
        latest = json.loads((self.root / 'data/interim/dataset_structure/latest.json').read_text())
        record = json.loads((self.root / latest['structure']).read_text())
        self.assertEqual((record['model_calls'], latest['summary']['tables'], latest['summary']['held']), (0, 8, 1))
        self.assertEqual(latest['document_sha256'], hashlib.sha256(doc.read_bytes()).hexdigest())

    def test_stale_receipt_is_flagged_and_profiling_can_be_restricted(self):
        lock = self.root / 'configs/research_resources.lock.json'
        write(lock, {'schema_version': 1, 'files': [{'id': 'extra', 'path': 'x', 'sha256': 'y', 'bytes': 1}] * 3})
        doc, data = module.write_document(self.root, only={'GATED'})
        text = doc.read_text()
        self.assertIn('**The catalogue receipt is older than the holdings:** the acquisition lock lists 3 files but the receipt knows 10.', text)
        self.assertIn('Not profiled in this run (restricted to other datasets)', text)
        self.assertEqual(next(d for d in data['datasets'] if d['id'] == 'HELD_DEMO')['tables'], 0)

    def test_second_census_reuses_unchanged_profiles_and_notices_changed_bytes(self):
        first_doc, first = module.write_document(self.root)
        self.assertEqual(first['reused_artifacts'], 0)
        first_text = first_doc.read_text()  # the same page is rewritten by every census
        second_doc, second = module.write_document(self.root)
        self.assertEqual(second['reused_artifacts'], len(self.artifacts), 'same bytes, options and code: every profile is reused')
        strip = lambda d: {k: v for k, v in d.items() if k != 'preaccess'}  # noqa: E731
        self.assertEqual([strip(d) for d in first['datasets']], [strip(d) for d in second['datasets']], 'a reused census is the same census')
        self.assertIn('file profiles were reused from the previous census', second_doc.read_text())
        self.assertNotIn('file profiles were reused', first_text)
        record = json.loads((self.root / json.loads((self.root / 'data/interim/dataset_structure/latest.json').read_text())['structure']).read_text())
        self.assertEqual((record['reused_artifacts'], record['code_sha256']), (len(self.artifacts), module.code_identity()))
        # One file changes and the receipt follows it: only that file is profiled again.
        changed = self.root / 'data/acquisitions/datasets/demo/hosp/admissions.csv.gz'
        changed.write_bytes(gzip.compress((CSV + '4,105,2152-01-01 00:00:00,URGENT,2,false\n').encode()))
        for a in self.artifacts:
            if a['path'].endswith('admissions.csv.gz'):
                a.update(bytes=changed.stat().st_size, sha256=hashlib.sha256(changed.read_bytes()).hexdigest())
        selection = self.root / 'data/interim/dataset_selection/20260102T000000000000Z/selection.json'
        write(selection, {'datasets': self.datasets})
        write(self.root / 'data/interim/dataset_selection/latest.json', {'selection': str(selection.relative_to(self.root)), 'selection_sha256': hashlib.sha256(selection.read_bytes()).hexdigest(),
                                                                          'summary': {'rechecked_unique_artifacts': len(self.artifacts)}})
        _, third = module.write_document(self.root)
        self.assertEqual(third['reused_artifacts'], len(self.artifacts) - 1)
        table = next(t for d in third['datasets'] for a in d['artifacts'] if a['path'].endswith('admissions.csv.gz') for t in a['tables'])
        self.assertEqual(table['rows'], 5, 'the changed file was profiled from its new bytes')
        # Other options, other profiling code or --no-reuse profile everything again.
        self.assertEqual(module.write_document(self.root, {'max_rows': 2})[1]['reused_artifacts'], 0)
        with patch.object(module, 'code_identity', return_value='other-code'):
            self.assertEqual(module.write_document(self.root)[1]['reused_artifacts'], 0)
        self.assertEqual(module.write_document(self.root, reuse=False)[1]['reused_artifacts'], 0)

    def test_another_study_census_of_the_same_bytes_is_reused_by_hash(self):
        import shutil
        _, first = module.write_document(self.root)
        own = self.root / 'data/interim/dataset_structure'
        neighbour = self.root.parent / (self.root.name + '-neighbour-study')  # a study workspace beside this one that profiled the same bytes
        self.addCleanup(shutil.rmtree, neighbour, True)
        write(neighbour / 'study.json', {'schema_version': 1, 'id': 'neighbour-study'})
        shutil.copytree(own, neighbour / 'data/interim/dataset_structure')
        shutil.rmtree(own)  # this study has no census of its own any more
        doc, second = module.write_document(self.root)
        self.assertEqual((second['reused_artifacts'], second['reused_from_other_studies']), (len(self.artifacts), len(self.artifacts)))
        strip = lambda d: {k: v for k, v in d.items() if k != 'preaccess'}  # noqa: E731
        self.assertEqual([strip(d) for d in first['datasets']], [strip(d) for d in second['datasets']], 'the neighbour’s profiles give the same census')
        self.assertIn("of them from other studies' censuses of the same bytes", doc.read_text())
        record = json.loads((neighbour / json.loads((neighbour / 'data/interim/dataset_structure/latest.json').read_text())['structure']).read_text())
        record['code_sha256'] = 'other-code'
        (neighbour / json.loads((neighbour / 'data/interim/dataset_structure/latest.json').read_text())['structure']).write_text(json.dumps(record))
        shutil.rmtree(own)
        self.assertEqual(module.write_document(self.root)[1]['reused_artifacts'], 0, 'a census made by other profiling code is never reused')

    def test_dated_report_is_tracked_and_registered_with_the_study(self):
        doc, data = module.write_document(self.root)
        sys.path.insert(0, str(CODE_ROOT / 'Scripts'))
        import register_research_study
        from report_dataset_status import save_report
        with patch.object(register_research_study, 'set_active', lambda *_: None):  # the fixture study has no folder under studies/
            target = save_report(self.root, doc, title='Data structure: tables, columns, types and statistics', stem='dataset-structure', runs_dir='structure_reports')
        self.assertEqual(target.name, f'dataset-structure-{date.today().isoformat()}.md')
        expected = doc.read_text().replace('](DATASET_SELECTION.md#', '](../docs/DATASET_SELECTION.md#').replace('](DATA_LICENSES_AND_AI_USE.md)', '](../docs/DATA_LICENSES_AND_AI_USE.md)')
        self.assertEqual(target.read_text(), expected, 'the saved copy keeps the text; its sibling links point at docs/ so the site build resolves them from reports/')
        self.assertIn('](../docs/DATASET_SELECTION.md#held_demo)', target.read_text())
        self.assertIn('](../docs/DATA_LICENSES_AND_AI_USE.md)', target.read_text())
        self.assertNotIn('](DATASET_SELECTION.md#', target.read_text())
        reports = json.loads((self.root / 'configs/research_studies.json').read_text())['studies'][0]['reports']
        self.assertEqual([(r['path'], r['title'].split(' (')[0]) for r in reports], [(str(target.relative_to(self.root)), 'Data structure: tables, columns, types and statistics')])
        self.assertTrue(list((self.root / 'runs/structure_reports').glob('*/study_update.json')))
        log = [json.loads(l)['action'] for l in (self.root / 'actions.jsonl').read_text().splitlines()]
        self.assertIn('report_added', log)


if __name__ == '__main__':
    unittest.main()
