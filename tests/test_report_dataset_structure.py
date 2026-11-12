"""The data-structure report profiles the held files deterministically and tells what is known before access; offline fixtures."""
from datetime import date, datetime
import csv
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

CODE_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('report_dataset_structure', CODE_ROOT / 'Scripts/report_dataset_structure.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
table_name = module.table_name


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
        self.assertEqual([(s['path'].split('/')[-1], s['reason']) for s in held['skipped']], [('blob.csv', 'binary_content_not_a_text_table'), ('results.parquet', 'not_a_parquet_file')], 'a file named .parquet is opened by the Parquet reader')
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
        self.assertIn('Not profiled: 1 file(s): binary content, not a text table (`blob.csv`); 1 file(s): not a Parquet file (`results.parquet`).', text)
        self.assertIn('| `hosp/` | 1 | 177 B | 1 | 1 .csv.gz |', text, 'the composition lists the folders of the release')
        self.assertIn('[DATASET_VARIABLES.csv](DATASET_VARIABLES.csv)', text)
        self.assertTrue((self.root / 'docs/DATASET_VARIABLES.csv').is_file())
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



# ------------------------------------------------------------------ fixtures for the readers: files built byte by byte, no third-party writer
def thrift_varint(n):
    out = bytearray()
    while True:
        byte, n = n & 0x7f, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def thrift_zigzag(n):
    return thrift_varint((n << 1) ^ (n >> 63))


def thrift_value(kind, value):
    if kind in (5, 6):
        return thrift_zigzag(value)
    if kind == 8:
        return thrift_varint(len(value)) + value
    if kind == 12:
        return thrift_struct(value)
    if kind == 9:
        element, items = value
        return bytes([(len(items) << 4) | element]) + b''.join(thrift_value(element, i) for i in items)
    raise ValueError(kind)


def thrift_struct(fields):
    """fields: [(id, kind, value)] in id order; kind 1 is a boolean field."""
    out, last = bytearray(), 0
    for field, kind, value in fields:
        code = (1 if value else 2) if kind == 1 else kind
        out.append(((field - last) << 4) | code)
        if kind != 1:
            out += thrift_value(kind, value)
        last = field
    return bytes(out) + b'\x00'


def parquet_file(rows=5):
    """A Parquet file with a valid footer: subject_id INT64 (2 nulls, 10..90), code STRING, time TIMESTAMP(us)."""
    leaf = lambda name, physical, extra=(): thrift_struct([(1, 5, physical), (3, 5, 1), (4, 8, name)] + list(extra))  # noqa: E731
    schema = [thrift_struct([(4, 8, b'schema'), (5, 5, 3)]), leaf(b'subject_id', 2), leaf(b'code', 6, [(6, 5, 0), (10, 12, [(1, 12, [])])]),
              leaf(b'time', 2, [(10, 12, [(8, 12, [(1, 1, True), (2, 12, [(2, 12, [])])])])])]
    def chunk(path, physical, stats):
        meta = [(1, 5, physical), (2, 9, (5, [0])), (3, 9, (8, [path])), (4, 5, 0), (5, 6, rows), (6, 6, 10), (7, 6, 10), (9, 6, 4)] + ([(12, 12, stats)] if stats else [])
        return [(2, 6, 4), (3, 12, meta)]
    micro = lambda iso: int((datetime.fromisoformat(iso) - datetime(1970, 1, 1)).total_seconds() * 1_000_000)  # noqa: E731
    group = [(1, 9, (12, [chunk(b'subject_id', 2, [(3, 6, 2), (5, 8, struct.pack('<q', 90)), (6, 8, struct.pack('<q', 10))]),
                          chunk(b'code', 6, [(3, 6, 0), (5, 8, b'zzz'), (6, 8, b'aaa')]),
                          chunk(b'time', 2, [(3, 6, 1), (5, 8, struct.pack('<q', micro('2150-02-01 08:30:00'))), (6, 8, struct.pack('<q', micro('2150-01-01 00:00:00')))])])),
             (2, 6, 30), (3, 6, rows)]
    footer = thrift_struct([(1, 5, 1), (2, 9, (12, [None] * 0)), (3, 6, rows), (4, 9, (12, []))])  # placeholder, replaced below
    body = bytearray()
    body.append((1 << 4) | 5); body += thrift_zigzag(1)
    body.append((1 << 4) | 9); body.append((len(schema) << 4) | 12); body += b''.join(schema)
    body.append((1 << 4) | 6); body += thrift_zigzag(rows)
    body.append((1 << 4) | 9); body.append((1 << 4) | 12); body += thrift_struct(group)
    footer = bytes(body) + b'\x00'
    return b'PAR1' + b'\x00' * 16 + footer + struct.pack('<I', len(footer)) + b'PAR1'


def ibm(value):
    if value == 0:
        return b'\x00' * 8
    sign, value, exponent = (0x80 if value < 0 else 0), abs(value), 0
    while value >= 1:
        value, exponent = value / 16, exponent + 1
    while value < 1 / 16:
        value, exponent = value * 16, exponent - 1
    return bytes([sign | (exponent + 64)]) + int(round(value * (1 << 56))).to_bytes(7, 'big')


def xpt_file():
    """SAS transport v5 with one dataset DM: USUBJID char(8), BRTHDT num DATE9., AGE num; the second subject's birth date is missing."""
    record = lambda text: (text.encode('latin-1') if isinstance(text, str) else text).ljust(80, b' ')  # noqa: E731
    out = record('HEADER RECORD*******LIBRARY HEADER RECORD!!!!!!!000000000000000000000000000000')
    out += record('SAS     SAS     SASLIB  9.4     X64_10PR' + ' ' * 24 + '01JAN20:00:00:00') + record('01JAN20:00:00:00')
    out += record('HEADER RECORD*******MEMBER  HEADER RECORD!!!!!!!000000000000000001600000000140')
    out += record('HEADER RECORD*******DSCRPTR HEADER RECORD!!!!!!!000000000000000000000000000000')
    out += record('SAS     DM      SASDATA 9.4     X64_10PR' + ' ' * 24 + '01JAN20:00:00:00')
    out += record('01JAN20:00:00:00' + ' ' * 16 + 'Demographics'.ljust(40) + 'DATA    ')
    out += record('HEADER RECORD*******NAMESTR HEADER RECORD!!!!!!!0000000003' + '0' * 20)
    variables = [(2, 8, 1, 'USUBJID', 'Unique Subject Identifier', '', 0), (1, 8, 2, 'BRTHDT', 'Date of Birth', 'DATE', 8), (1, 8, 3, 'AGE', 'Age', '', 16)]
    names = b''.join(struct.pack('>hhhh8s40s8shhh2s8shhl52s', t, 0, n, v, name.encode().ljust(8), label.encode().ljust(40), fmt.encode().ljust(8), 9 if fmt else 0, 0, 0, b'\x00\x00', b' ' * 8, 0, 0, pos, b'\x00' * 52)
                     for t, n, v, name, label, fmt, pos in variables)
    out += names.ljust(-(-len(names) // 80) * 80, b'\x00')
    out += record('HEADER RECORD*******OBS     HEADER RECORD!!!!!!!000000000000000000000000000000')
    born = (datetime(1950, 3, 15) - datetime(1960, 1, 1)).days
    data = b'01-001  ' + ibm(born) + ibm(76) + b'01-002  ' + b'.' + b'\x00' * 7 + ibm(70.5)
    return out + data.ljust(-(-len(data) // 80) * 80, b' ')


MAIN = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def xlsx_file(path):
    with zipfile.ZipFile(path, 'w') as book:
        book.writestr('[Content_Types].xml', '<Types/>')
        book.writestr('xl/workbook.xml', f'<workbook {MAIN} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                                         '<sheet name="Labs" sheetId="1" r:id="rId1"/><sheet name="Empty" sheetId="2" r:id="rId2"/></sheets></workbook>')
        book.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                                     '<Relationship Id="rId1" Type="w" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="w" Target="/xl/worksheets/sheet2.xml"/></Relationships>')
        book.writestr('xl/sharedStrings.xml', f'<sst {MAIN}><si><t>patient</t></si><si><t>test</t></si><si><r><t>res</t></r><r><t>ult</t></r></si><si><t>taken</t></si>'
                                              '<si><t>HGB</t></si><si><t>WBC</t></si><si><t>Lab results 2023</t></si></sst>')
        book.writestr('xl/styles.xml', f'<styleSheet {MAIN}><numFmts count="1"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/></numFmts>'
                                       '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs></styleSheet>')
        cells = lambda r, *cs: f'<row r="{r}">' + ''.join(cs) + '</row>'  # noqa: E731
        book.writestr('xl/worksheets/sheet1.xml', f'<worksheet {MAIN}><sheetData>'
                      + cells(2, '<c r="A2" t="s"><v>0</v></c>', '<c r="B2" t="s"><v>1</v></c>', '<c r="C2" t="s"><v>2</v></c>', '<c r="D2" t="s"><v>3</v></c>')
                      + cells(3, '<c r="A3"><v>1</v></c>', '<c r="B3" t="s"><v>4</v></c>', '<c r="C3"><v>13.5</v></c>', '<c r="D3" s="1"><v>45000</v></c>')
                      + cells(4, '<c r="A4"><v>2</v></c>', '<c r="B4" t="s"><v>5</v></c>', '<c r="D4" s="1"><v>45001.5</v></c>')
                      + cells(5, '<c r="A5" t="inlineStr"><is><t>3</t></is></c>', '<c r="B5" t="s"><v>4</v></c>', '<c r="C5" t="e"><v>#N/A</v></c>')
                      + '</sheetData></worksheet>')
        book.writestr('xl/worksheets/sheet2.xml', f'<worksheet {MAIN}><sheetData/></worksheet>')


def workspace_with(root, files):
    """A study workspace whose one held dataset HELD consists of exactly these files (relative path -> bytes)."""
    write(root / 'study.json', {'schema_version': 1, 'id': 'demo-study'})
    write(root / 'configs/research_studies.json', {'schema_version': 1, 'default_study': 'demo-study', 'studies': [
        {'id': 'demo-study', 'name': 'Demo study', 'question': 'q', 'intended_use': 'u', 'population': '', 'targets': '', 'inclusion': '', 'exclusion': '',
         'dataset_ids': [], 'dataset_annotations': {}, 'study_annotations': {}}]})
    artifacts = []
    for rel, content in files.items():
        path = root / 'data/acquisitions/datasets/held' / rel
        write(path, content)
        artifacts.append({'path': str(path.relative_to(root)), 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    selection = root / 'data/interim/dataset_selection/20260101T000000000000Z/selection.json'
    write(selection, {'datasets': [{'id': 'HELD', 'name': 'Held release', 'category': 'clinical_sources', 'access_route': 'public_no_registration', 'registration_url': None, 'scope': '',
                                    'source_urls': [], 'completeness': 'complete_public_release', 'obtained_artifacts': artifacts, 'source_locators': ['data/acquisitions/datasets/held/']}]})
    write(root / 'data/interim/dataset_selection/latest.json', {'selection': str(selection.relative_to(root)), 'selection_sha256': hashlib.sha256(selection.read_bytes()).hexdigest(),
                                                               'summary': {'rechecked_unique_artifacts': len(artifacts)}})
    write(root / 'configs/research_resources.lock.json', {'schema_version': 1, 'files': [{'id': f'f{i}', **a} for i, a in enumerate(artifacts)]})
    return artifacts


class ReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def held(self, files, **options):
        workspace_with(self.root, files)
        return next(d for d in module.collect(self.root, options or None)['datasets'] if d['id'] == 'HELD')

    def tables(self, held):
        return {table_name(a['path'], t): t for a in held['artifacts'] for t in a['tables']}

    def test_excel_sheets_are_profiled_with_their_header_row_and_dates(self):
        buffer = self.root.parent / (self.root.name + '-book.xlsx')
        self.addCleanup(buffer.unlink, True)
        xlsx_file(buffer)
        held = self.held({'labs.xlsx': buffer.read_bytes()})
        [(name, sheet)] = self.tables(held).items()
        self.assertTrue(name.endswith('labs.xlsx → Labs'), 'one table per sheet with data; the empty sheet is not a table')
        self.assertEqual((sheet['format'], sheet['rows']), ('Excel sheet', 3))
        columns = {c['name']: c for c in sheet['columns']}
        self.assertEqual(list(columns), ['patient', 'test', 'result', 'taken'], 'the first non-empty row is the header; rich text and shared strings are joined')
        self.assertEqual((columns['patient']['type'], columns['result']['type'], columns['result']['missing']), ('integer', 'number', 2), 'error and absent cells are missing')
        self.assertEqual(columns['test']['labels'], [{'value': 'HGB', 'count': 2}, {'value': 'WBC', 'count': 1}])
        self.assertEqual((columns['taken']['type'], columns['taken']['time_range']), ('datetime', ['2023-03-15', '2023-03-16 12:00:00']), 'date-formatted serials become dates')

    def test_parquet_is_described_from_its_footer(self):
        held = self.held({'data/part-0.parquet': parquet_file()})
        [table] = self.tables(held).values()
        self.assertEqual((table['format'], table['rows'], table['statistics'], table['row_groups']), ('Parquet', 5, 'footer', 1))
        columns = {c['name']: c for c in table['columns']}
        self.assertEqual((columns['subject_id']['declared_type'], columns['subject_id']['type'], columns['subject_id']['values'], columns['subject_id']['missing']), ('INT64', 'integer', 3, 2))
        self.assertEqual((columns['subject_id']['numeric']['min'], columns['subject_id']['numeric']['max']), (10, 90))
        self.assertEqual((columns['code']['declared_type'], columns['code']['type']), ('BYTE_ARRAY/STRING', 'text'))
        self.assertEqual(module.parquet_leaves([{5: 1}, {3: 1, 4: b'codes', 5: 1}, {3: 2, 4: b'list', 5: 1}, {1: 6, 3: 1, 4: b'element'}]), [('codes.list.element', {1: 6, 3: 1, 4: b'element'}, True)],
                         'a leaf under a repeated group counts list elements, not rows')
        self.assertNotIn('numeric', columns['code'], 'text minimum and maximum are cell values and are not copied')
        self.assertEqual((columns['time']['declared_type'], columns['time']['type'], columns['time']['time_range']),
                         ('INT64/TIMESTAMP(us)', 'datetime', ['2150-01-01 00:00:00', '2150-02-01 08:30:00']))
        doc, _ = module.write_document(self.root)
        self.assertIn("schema and the writer's footer statistics, values not read", doc.read_text())

    def test_sas_transport_datasets_keep_their_labels_and_dates_and_an_html_page_is_reported(self):
        held = self.held({'sdtm/dm.xpt': xpt_file(), 'refs/ranges.xpt': b'<HTML><HEAD><TITLE>404 Not Found</TITLE></HEAD></HTML>'})
        [(name, table)] = self.tables(held).items()
        self.assertTrue(name.endswith('dm.xpt → DM'))
        self.assertEqual((table['format'], table['rows'], table['dataset_label']), ('SAS transport (XPORT v5)', 2, 'Demographics'))
        columns = {c['name']: c for c in table['columns']}
        self.assertEqual({n: (c['label'], c['declared_type'], c['type']) for n, c in columns.items()},
                         {'USUBJID': ('Unique Subject Identifier', 'char(8)', 'text'), 'BRTHDT': ('Date of Birth', 'num(8) DATE', 'date'), 'AGE': ('Age', 'num(8)', 'number')})
        self.assertEqual((columns['BRTHDT']['time_range'], columns['BRTHDT']['missing']), (['1950-03-15', '1950-03-15'], 1), 'SAS dates count days from 1960; a SAS missing value is missing')
        self.assertEqual((columns['AGE']['numeric']['min'], columns['AGE']['numeric']['max']), (70.5, 76.0))
        self.assertEqual([s['reason'] for s in held['skipped']], ['content_is_an_html_page_not_sas_transport'])
        text = module.write_document(self.root)[0].read_text()
        self.assertIn('| `USUBJID` Unique Subject Identifier | char(8) → text |', text)
        self.assertIn('an HTML page saved under a SAS transport name (upstream content, not data) (`ranges.xpt`)', text)

    def test_delimited_text_tables_are_recognised_and_prose_is_not(self):
        held = self.held({'set-a/1001.txt': 'Time,Parameter,Value\n00:00,Age,54\n00:07,HR,73\n00:37,HR,77\n', 'set-a/1002.txt': 'Time,Parameter,Value\n00:00,Age,61\n01:00,HR,90\n',
                          'set-a/1003.txt': 'Time,Parameter,Value\n00:00,Age,48\n00:30,Temp,37.2\n', 'vitals.psv': 'HR|O2Sat|SepsisLabel\n80|97|0\n91|95|1\n',
                          'README.txt': 'a,b,c\n1,2,3\n4,5,6\n', 'notes.txt': 'This release, prepared by us, contains\nthree folders of records\nand, a table of outcomes.\n'})
        tables = self.tables(held)
        self.assertEqual(sorted(n.rsplit('/', 1)[-1] for n in tables), ['1001.txt', '1002.txt', '1003.txt', 'vitals.psv'])
        self.assertEqual(next(t for n, t in tables.items() if n.endswith('vitals.psv'))['delimiter'], 'pipe')
        self.assertEqual(held['others'], {'.txt': 2}, 'a README and prose stay text')
        entries = module.families_of(held, module.DEFAULTS)
        family = next(e for e in entries if e['family'])
        self.assertEqual((len(family['names']), family['rows'], dict(family['folders']), family['distinct_names']), (3, 7, {'set-a': 3}, 3))
        self.assertEqual(family['names'][0], 'set-a/1001.txt', 'names are relative to the release folder')
        merged = {c['name']: c for c in family['columns']}
        self.assertEqual((merged['Value']['values'], merged['Value']['numeric']['min'], merged['Value']['numeric']['max']), (7, 37.2, 90.0))
        self.assertEqual(merged['Parameter']['labels'], [{'value': 'HR', 'count': 3}, {'value': 'Age', 'count': 3}, {'value': 'Temp', 'count': 1}], 'complete label lists merge exactly')
        text = module.write_document(self.root)[0].read_text()
        self.assertIn('3 tables with the same 3 columns', text)
        rows = list(csv.DictReader((self.root / 'docs/DATASET_VARIABLES.csv').open()))
        self.assertEqual([(r['column'], r['tables_with_these_columns']) for r in rows if r['format'] == 'delimited text' and r['tables_with_these_columns'] == '3'],
                         [('Time', '3'), ('Parameter', '3'), ('Value', '3')], 'the dictionary lists a family once')
        self.assertEqual(len(rows), 6)

    def test_copies_of_the_same_files_are_named_and_rendering_changes_keep_the_profiles(self):
        files = {f'set-a/{i}.txt': f'Time,Parameter,Value\n00:00,Age,{50 + i}\n00:10,HR,{70 + i}\n' for i in range(3)}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            for name, text in files.items():
                z.writestr(name, text)
        held = self.held({**files, 'set-a.zip': archive.getvalue()})
        [family] = [e for e in module.families_of(held, module.DEFAULTS) if e['family']]
        self.assertEqual((len(family['names']), family['distinct_names'], dict(family['folders'])), (6, 3, {'set-a': 3, 'set-a.zip → set-a': 3}))
        text = module.write_document(self.root)[0].read_text()
        self.assertIn('only 3 distinct file names: the same files appear in several folders or archives', text)
        self.assertIn("`set-a.zip → set-a` (3)", text)
        source = Path(module.__file__).read_text()
        marker = source.index(module.PROFILE_CODE_END)
        self.assertGreater(source.index('def render_dataset('), marker, 'page code sits below the marker')
        self.assertLess(source.index('def profile_artifact('), marker, 'profiling code sits above the marker')
        with patch.object(module.Path, 'read_text', lambda self, *a, **k: source[:marker] + source[marker:].replace('distinct file names', 'distinct names')):
            self.assertEqual(module.code_identity(), hashlib.sha256(source[:marker].encode()).hexdigest(), 'a change below the marker keeps the identity')

    def test_archive_metadata_is_ignored_and_large_members_are_streamed(self):
        archive = io.BytesIO()
        big = 'id,value\n' + ''.join(f'{i},{i * 2}\n' for i in range(5000))
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('release/big.csv', big)
            z.writestr('__MACOSX/release/._big.csv', b'\x00\x05\x16\x07binary resource fork')
            z.writestr('release/.DS_Store', b'\x00\x00\x00\x01Bud1')
            z.writestr('release/meta.json', json.dumps([{'k': i} for i in range(3000)]))
        workspace_with(self.root, {'release.zip': archive.getvalue()})
        write(self.root / 'configs/dataset_validation.json', {'limits': {'max_member_bytes_to_parse': 1024}})
        held = next(d for d in module.collect(self.root)['datasets'] if d['id'] == 'HELD')
        tables = self.tables(held)
        self.assertEqual(next(t for n, t in tables.items() if n.endswith('big.csv'))['rows'], 5000, 'a delimited member larger than the member budget is streamed')
        self.assertEqual([(s['member'], s['reason']) for s in held['skipped']], [('release/meta.json', 'member_over_parse_budget')], 'a JSON document is read whole and keeps the budget')
        self.assertEqual(held['others'], {'archive metadata': 2})
        text = module.write_document(self.root)[0].read_text()
        self.assertIn('2 archive metadata entries (macOS `__MACOSX/` resource forks, `.DS_Store`) are not data and were ignored.', text)
        self.assertIn('| [HELD](#held) | 1 (', text)
        self.assertTrue(text.split('| [HELD](#held) | 1 (')[1].split('\n')[0].endswith('| 1 | 5,000 | 2 | 0 | 1 |'), 'metadata entries are not counted as other files')

    def test_a_profile_cut_by_the_table_budget_is_never_reused(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            for i in range(3):
                z.writestr(f't{i}.csv', 'a,b\n1,2\n')
        workspace_with(self.root, {'tables.zip': archive.getvalue()})
        _, first = module.write_document(self.root, {'max_tables': 2})
        self.assertEqual(next(d for d in first['datasets'] if d['id'] == 'HELD')['tables'], 2)
        _, second = module.write_document(self.root, {'max_tables': 2})
        self.assertEqual(second['reused_artifacts'], 0, 'the cut profile depends on the budget left, so it is profiled again')
        _, third = module.write_document(self.root, {'max_tables': 5, 'rendered_tables': 7})
        self.assertEqual((third['reused_artifacts'], next(d for d in third['datasets'] if d['id'] == 'HELD')['tables']), (0, 3))
        _, fourth = module.write_document(self.root, {'max_tables': 50, 'rendered_columns': 9})
        self.assertEqual(fourth['reused_artifacts'], 1, 'an uncut profile does not depend on the table budget or the rendering options')
        _, fifth = module.write_document(self.root, {'max_tables': 50, 'label_levels': 3})
        self.assertEqual(fifth['reused_artifacts'], 0, 'the label limit changes the profile itself')

    def test_a_workbook_cut_by_the_table_budget_records_the_cut(self):
        buffer = self.root.parent / (self.root.name + '-two-sheets.xlsx')
        self.addCleanup(buffer.unlink, True)
        xlsx_file(buffer)
        with zipfile.ZipFile(buffer, 'a') as book:
            book.writestr('xl/worksheets/sheet2.xml', f'<worksheet {MAIN}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>x</t></is></c></row>'
                                                      '<row r="2"><c r="A2"><v>1</v></c></row></sheetData></worksheet>')
        held = self.held({'book.xlsx': buffer.read_bytes()}, max_tables=1)
        self.assertEqual((held['tables'], [s['reason'] for s in held['skipped']]), (1, ['table_budget_for_dataset']))




if __name__ == '__main__':
    unittest.main()
