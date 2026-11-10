#!/usr/bin/env python3
"""Write the data-structure report: tables, columns, types and descriptive statistics of the held files, and what is known
before access for the datasets that are not held.

Reads the current catalogue receipt (which verified, locked files each dataset holds), the validation registry (families,
size limits) and the licence registers (retained documentation), and writes docs/DATASET_STRUCTURE.md plus a JSON record
under data/interim/dataset_structure/<run>/:

  1. held datasets, one section each: every tabular file or archive member (CSV/TSV, gzip-compressed tables, ARFF, SQLite,
     NDJSON, record-list JSON, tables inside ZIP archives) as a table with its columns, declared or inferred types,
     missingness, distinct counts, numeric and date ranges and the labels of low-cardinality columns; the other files are
     counted by extension; files no adapter reads are listed with the reason;
  2. datasets that are not held (registration, agreement or licence decision pending, or an open release not yet acquired):
     the access route, the registry's scope statement, the same-family releases that ARE held (a provider's demo release
     shows the table and column layout of the gated release, not its counts), the retained public documentation, and what
     stays unknown until access.

Deterministic, offline, no model calls, standard library only. Statistics are aggregates: no cell values are copied except
the labels of columns with few distinct values. The document grants no data or AI-use permission; as a dictionary-like
derivative of the sources it is covered by the same source-bound preflight as the files it describes before any model
reads it. `--report` also saves a dated tracked copy under reports/ and registers it with the study.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import io
import json
import math
from pathlib import Path
import random
import re
import sqlite3
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root, require_workspace, workspace_study_id  # noqa: E402
from datahunter.research_studies import default_study  # noqa: E402

ROOT = workspace_root(default=CODE_ROOT)
NA_TOKENS = {'', 'na', 'n/a', 'null', 'none', 'nan', '?', 'nil'}
BOOL_TOKENS = {'true', 'false', 'yes', 'no'}
INT_RE = re.compile(r'^[+-]?\d+$')
FLOAT_RE = re.compile(r'^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$')
DATETIME_RE = re.compile(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TABLE_SUFFIXES = ('.csv', '.tsv', '.csv.gz', '.tsv.gz')
ADAPTER_MISSING = ('.parquet', '.feather', '.xlsx', '.xls', '.sav', '.dta', '.rds', '.rdata', '.h5', '.hdf5', '.mat', '.npy', '.npz', '.pkl', '.pickle', '.pt')
DEFAULT_LIMITS = {'max_decompressed_bytes_per_file': 512 * 1024 * 1024, 'max_archive_members': 100000, 'max_member_bytes_to_parse': 16 * 1024 * 1024}
DEFAULTS = {'max_rows': 200000, 'max_count_rows': 5000000, 'max_tables': 400, 'distinct_cap': 50000, 'top_cap': 200, 'reservoir': 2000, 'label_levels': 20, 'rendered_tables': 60, 'rendered_columns': 60}
csv.field_size_limit(16 * 1024 * 1024)


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def fmt_bytes(value):
    if value is None:
        return '—'
    for divisor, unit in [(1024 ** 3, 'GiB'), (1024 ** 2, 'MiB'), (1024, 'KiB')]:
        if value >= divisor:
            return f'{value / divisor:.1f} {unit}'
    return f'{value:,} B'


def fmt_num(value):
    if value is None:
        return '—'
    if isinstance(value, float):
        if value != value or math.isinf(value):
            return str(value)
        if abs(value) >= 1e6 or (abs(value) < 1e-3 and value != 0):
            return f'{value:.3g}'
        return f'{value:,.2f}'.rstrip('0').rstrip('.') if value != int(value) else f'{int(value):,}'
    return f'{value:,}'


CONTROL_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


def cell(value, limit=48):
    text = CONTROL_RE.sub('', str(value)).replace('|', '\\|').replace('\n', ' ').replace('`', "'")
    return text if len(text) <= limit else text[:limit - 1] + '…'


def classify_value(value):
    """The kind of one non-missing cell: integer, number, datetime, date, boolean, text, binary, object or array."""
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'number'
    if isinstance(value, (bytes, bytearray)):
        return 'binary'
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, list):
        return 'array'
    text = value.strip()
    if INT_RE.match(text):
        return 'integer'
    if FLOAT_RE.match(text):
        return 'number'
    if DATETIME_RE.match(text):
        return 'datetime'
    if DATE_RE.match(text):
        return 'date'
    if text.lower() in BOOL_TOKENS:
        return 'boolean'
    return 'text'


def is_missing(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in NA_TOKENS
    if isinstance(value, float):
        return value != value
    return False


class ColumnStats:
    """Aggregate statistics of one column; never keeps cell values apart from the labels of low-cardinality columns."""

    def __init__(self, name, declared=None, options=None):
        o = options or DEFAULTS
        self.name, self.declared = name, declared
        self.distinct_cap, self.top_cap, self.reservoir_size, self.label_levels = o['distinct_cap'], o['top_cap'], o['reservoir'], o['label_levels']
        self.n = self.missing = 0
        self.kinds = Counter()
        self.distinct, self.distinct_overflow = set(), False
        self.top = Counter()
        self.num_n, self.num_min, self.num_max, self.num_sum, self.num_sumsq = 0, None, None, 0.0, 0.0
        self.sample, self.seen_numeric = [], 0
        self.time_min = self.time_max = None
        self.len_min = self.len_max = None
        self.random = random.Random(7)

    def add(self, value):
        self.n += 1
        if is_missing(value):
            self.missing += 1
            return
        kind = classify_value(value)
        self.kinds[kind] += 1
        label = value.strip() if isinstance(value, str) else (kind if kind in {'object', 'array', 'binary'} else str(value))
        if kind in {'integer', 'number'}:
            x = float(label)
            if not math.isinf(x):
                self.num_n += 1
                self.num_min = x if self.num_min is None else min(self.num_min, x)
                self.num_max = x if self.num_max is None else max(self.num_max, x)
                self.num_sum += x
                self.num_sumsq += x * x
                self.seen_numeric += 1
                if len(self.sample) < self.reservoir_size:
                    self.sample.append(x)
                else:
                    j = self.random.randrange(self.seen_numeric)
                    if j < self.reservoir_size:
                        self.sample[j] = x
        elif kind in {'datetime', 'date'}:
            self.time_min = label if self.time_min is None else min(self.time_min, label)
            self.time_max = label if self.time_max is None else max(self.time_max, label)
        elif kind == 'text':
            length = len(label)
            self.len_min = length if self.len_min is None else min(self.len_min, length)
            self.len_max = length if self.len_max is None else max(self.len_max, length)
        if not self.distinct_overflow:
            if len(self.distinct) < self.distinct_cap or label in self.distinct:
                self.distinct.add(label)
            else:
                self.distinct_overflow = True
        if self.top is not None:
            self.top[label] += 1
            if len(self.top) > self.top_cap:
                self.top = None

    def inferred_type(self):
        present = self.n - self.missing
        if not present:
            return 'empty'
        kinds = self.kinds
        if kinds['integer'] + kinds['number'] == present:
            return 'integer' if not kinds['number'] else 'number'
        if kinds['datetime'] + kinds['date'] == present:
            return 'datetime' if kinds['datetime'] else 'date'
        if len(kinds) == 1:
            return next(iter(kinds))
        return 'mixed (' + ', '.join(k for k, _ in kinds.most_common(2)) + ')'

    def summary(self):
        present = self.n - self.missing
        distinct = None if self.distinct_overflow else len(self.distinct)
        kind = self.inferred_type()
        out = {'name': self.name, 'declared_type': self.declared, 'type': kind, 'values': present, 'missing': self.missing,
               'distinct': distinct, 'distinct_at_least': len(self.distinct) if self.distinct_overflow else None,
               'key_like': bool(present > 1 and distinct == present)}
        if self.num_n:
            mean = self.num_sum / self.num_n
            variance = max(self.num_sumsq / self.num_n - mean * mean, 0.0)
            ordered = sorted(self.sample)
            out['numeric'] = {'min': self.num_min, 'max': self.num_max, 'mean': mean, 'sd': math.sqrt(variance),
                              'median': ordered[len(ordered) // 2] if ordered else None, 'median_is_estimate': self.seen_numeric > len(self.sample)}
        if self.time_min is not None:
            out['time_range'] = [self.time_min, self.time_max]
        if self.len_min is not None:
            out['text_length'] = [self.len_min, self.len_max]
        if self.top is not None and distinct is not None and distinct <= self.label_levels and kind not in {'integer', 'number', 'datetime', 'date'}:
            out['labels'] = [{'value': v, 'count': c} for v, c in self.top.most_common(self.label_levels)]
        elif self.top is not None and kind == 'boolean':
            out['labels'] = [{'value': v, 'count': c} for v, c in self.top.most_common()]
        return out


def profile_rows(header, rows, options, declared=None):
    """Column statistics over the first max_rows rows; the row count continues to max_count_rows."""
    columns = [ColumnStats(name, (declared or {}).get(name), options) for name in header]
    n = ragged = 0
    truncated = False
    for row in rows:
        n += 1
        if n <= options['max_rows']:
            if len(row) != len(columns):
                ragged += 1
            for column, value in zip(columns, row):
                column.add(value)
        elif n >= options['max_count_rows']:
            truncated = True
            break
    return {'rows': n, 'rows_are_lower_bound': truncated, 'profiled_rows': min(n, options['max_rows']), 'ragged_rows': ragged,
            'columns': [c.summary() for c in columns]}


def text_stream(binary, name):
    opener = gzip.open(binary, 'rb') if name.lower().endswith('.gz') else binary
    head = opener.peek(2)[:2] if hasattr(opener, 'peek') else b''
    encoding = 'utf-16' if head in (b'\xff\xfe', b'\xfe\xff') else 'utf-8-sig'
    return io.TextIOWrapper(opener, encoding=encoding, errors='replace', newline='')


def delimiter_for(name, first_line):
    if name.lower().endswith(('.tsv', '.tsv.gz')):
        return '\t'
    return ';' if first_line.count(';') > first_line.count(',') else ','


def profile_csv(stream, name, options):
    first = stream.readline()
    while first and (not first.strip() or first.startswith('#')):
        first = stream.readline()
    if not first:
        return {'state': 'skipped', 'reason': 'empty_file'}
    if first.lstrip().lower().startswith(('<!doctype html', '<html')):
        return {'state': 'skipped', 'reason': 'html_is_not_tabular_data'}
    if '\x00' in first:
        return {'state': 'skipped', 'reason': 'binary_content_not_a_text_table'}
    delimiter = delimiter_for(name, first)
    reader = csv.reader(io.StringIO(first), delimiter=delimiter)
    header = next(reader)
    if len(header) < 2 and delimiter == ',' and first.count('\t') >= 1:
        delimiter, header = '\t', next(csv.reader(io.StringIO(first), delimiter='\t'))
    body = csv.reader(stream, delimiter=delimiter)
    profile = profile_rows(header, (row for row in body if row), options)
    return {'state': 'profiled', 'format': 'delimited text', 'delimiter': 'tab' if delimiter == '\t' else delimiter, **profile}


def parse_arff_attribute(line):
    rest = line[len('@attribute'):].strip()
    if rest.startswith(("'", '"')):
        quote = rest[0]
        end = rest.index(quote, 1)
        name, kind = rest[1:end], rest[end + 1:].strip()
    else:
        parts = rest.split(None, 1)
        name, kind = parts[0], parts[1].strip() if len(parts) > 1 else ''
    if kind.startswith('{'):
        levels = [v.strip().strip("'\"") for v in next(csv.reader(io.StringIO(kind.strip('{}')), skipinitialspace=True))]
        return name, f'nominal ({len(levels)} levels)', levels
    base = kind.split()[0].lower() if kind else 'unknown'
    return name, {'numeric': 'number', 'real': 'number', 'integer': 'integer', 'string': 'text', 'date': 'date'}.get(base, base), None


def profile_arff(stream, options):
    names, declared, levels, relation = [], {}, {}, None
    for line in stream:
        stripped = line.strip()
        if not stripped or stripped.startswith('%'):
            continue
        lowered = stripped.lower()
        if lowered.startswith('@relation'):
            relation = stripped.split(None, 1)[1].strip("'\"") if ' ' in stripped else None
        elif lowered.startswith('@attribute'):
            name, kind, values = parse_arff_attribute(stripped)
            names.append(name)
            declared[name] = kind
            if values is not None:
                levels[name] = values
        elif lowered.startswith('@data'):
            break
    if not names:
        return {'state': 'skipped', 'reason': 'no_arff_attributes'}
    data_lines = (line for line in stream if line.strip() and not line.lstrip().startswith('%'))
    first = next(data_lines, None)
    if first is None:
        profile = profile_rows(names, iter(()), options, declared)
    elif first.lstrip().startswith('{'):
        count = 1 + sum(1 for _ in data_lines)
        profile = {'rows': count, 'rows_are_lower_bound': False, 'profiled_rows': 0, 'ragged_rows': 0, 'sparse': True,
                   'columns': [{'name': n, 'declared_type': declared[n], 'type': 'not profiled (sparse ARFF)', 'values': None, 'missing': None, 'distinct': None, 'distinct_at_least': None, 'key_like': False} for n in names]}
    else:
        def rows():
            yield from csv.reader([first], quotechar="'", skipinitialspace=True)
            yield from csv.reader(data_lines, quotechar="'", skipinitialspace=True)
        profile = profile_rows(names, rows(), options, declared)
    return {'state': 'profiled', 'format': 'ARFF', 'relation': relation, **profile}


def profile_sqlite(path, options):
    tables = []
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names[:options['max_tables']]:
            quoted = '"' + name.replace('"', '""') + '"'
            info = connection.execute(f'PRAGMA table_info({quoted})').fetchall()
            header = [row[1] for row in info]
            declared = {row[1]: (row[2] or 'untyped').lower() for row in info}
            count = connection.execute(f'SELECT COUNT(*) FROM {quoted}').fetchone()[0]
            cursor = connection.execute(f'SELECT * FROM {quoted} LIMIT {int(options["max_rows"])}')
            profile = profile_rows(header, cursor, {**options, 'max_count_rows': options['max_rows'] + 1}, declared)
            profile['rows'], profile['rows_are_lower_bound'] = count, False
            tables.append({'state': 'profiled', 'format': 'SQLite table', 'member': name, **profile})
    finally:
        connection.close()
    return tables


def profile_records(stream, options, kind):
    """NDJSON lines or a JSON list of objects: top-level keys as columns."""
    def objects():
        if kind == 'ndjson':
            for line in stream:
                if line.strip():
                    yield json.loads(line)
        else:
            yield from stream
    keys, first_rows = [], []
    probe = objects()
    for item in probe:
        if not isinstance(item, dict):
            return {'state': 'skipped', 'reason': 'json_items_are_not_objects'}
        first_rows.append(item)
        for key in item:
            if key not in keys:
                keys.append(key)
        if len(first_rows) >= 1000:
            break
    if not first_rows:
        return {'state': 'skipped', 'reason': 'no_records'}
    def rows():
        for item in first_rows:
            yield [item.get(k) for k in keys]
        for item in probe:
            if isinstance(item, dict):
                yield [item.get(k) for k in keys]
    profile = profile_rows(keys, rows(), options)
    out = {'state': 'profiled', 'format': 'NDJSON records' if kind == 'ndjson' else 'JSON record list', **profile}
    if 'resourceType' in keys:
        types = Counter(item.get('resourceType') for item in first_rows)
        out['resource_types'] = dict(types.most_common(5))
    return out


def file_kind(name):
    lowered = name.lower()
    if lowered.endswith(TABLE_SUFFIXES):
        return 'table'
    if lowered.endswith(('.arff', '.sparse_arff')):
        return 'arff'
    if lowered.endswith(('.sqlite', '.sqlite3', '.db', '.sqlite.gz', '.sqlite3.gz', '.db.gz')):
        return 'sqlite'
    if lowered.endswith(('.ndjson', '.jsonl', '.ndjson.gz', '.jsonl.gz')):
        return 'ndjson'
    if lowered.endswith('.json'):
        return 'json'
    if lowered.endswith('.zip'):
        return 'zip'
    if lowered.endswith(ADAPTER_MISSING) or lowered.endswith(tuple(s + '.gz' for s in ADAPTER_MISSING)):
        return 'adapter_missing'
    return 'other'


def extension(name):
    lowered = name.lower()
    for suffix in ('.csv.gz', '.tsv.gz', '.ndjson.gz', '.json.gz', '.txt.gz', '.sqlite3.gz', '.tar.gz'):
        if lowered.endswith(suffix):
            return suffix
    return Path(lowered).suffix or '(none)'


def decompress_to_temp(path, budget):
    """Decompress a gzip file for a reader that needs a file name; refuse once the budget is exceeded."""
    target = tempfile.NamedTemporaryFile(prefix='datahunter_structure_', suffix='.sqlite', delete=False)
    total = 0
    with gzip.open(path, 'rb') as stream, target:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            total += len(block)
            if total > budget:
                Path(target.name).unlink(missing_ok=True)
                return None
            target.write(block)
    return Path(target.name)


def profile_json_file(binary, name, size, options, limits):
    if size > limits['max_member_bytes_to_parse']:
        return {'state': 'skipped', 'reason': 'json_document_over_parse_budget'}
    with text_stream(binary, name) as stream:
        document = json.load(stream)
    if isinstance(document, list):
        return profile_records(iter(document), options, 'json')
    return {'state': 'document', 'format': 'JSON document', 'top_level_keys': len(document) if isinstance(document, dict) else None}


def profile_zip(path, options, limits, budget_tables):
    tables, others, skipped = [], Counter(), []
    with zipfile.ZipFile(path) as archive:
        for index, info in enumerate(archive.infolist()):
            if index >= limits['max_archive_members']:
                skipped.append({'member': '…', 'reason': 'archive_member_budget'})
                break
            if info.is_dir():
                continue
            member = info.filename
            kind = file_kind(member)
            if kind in {'table', 'arff', 'ndjson', 'json'}:
                if len(tables) >= budget_tables:
                    skipped.append({'member': member, 'reason': 'table_budget_for_dataset'})
                    continue
                if info.file_size > limits['max_member_bytes_to_parse']:
                    skipped.append({'member': member, 'bytes': info.file_size, 'reason': 'member_over_parse_budget'})
                    continue
                try:
                    with archive.open(info) as binary:
                        if kind == 'table':
                            with text_stream(binary, member) as stream:
                                result = profile_csv(stream, member, options)
                        elif kind == 'arff':
                            with text_stream(binary, member) as stream:
                                result = profile_arff(stream, options)
                        elif kind == 'ndjson':
                            with text_stream(binary, member) as stream:
                                result = profile_records(stream, options, 'ndjson')
                        else:
                            result = profile_json_file(binary, member, info.file_size, options, limits)
                except (UnicodeError, csv.Error, ValueError, OSError, zipfile.BadZipFile) as error:
                    result = {'state': 'skipped', 'reason': 'parse_failed: ' + type(error).__name__}
                if result['state'] == 'profiled':
                    tables.append({'member': member, 'bytes': info.file_size, **result})
                elif result['state'] == 'document':
                    others['json document'] += 1
                else:
                    skipped.append({'member': member, 'reason': result['reason']})
            elif kind == 'adapter_missing':
                skipped.append({'member': member, 'bytes': info.file_size, 'reason': 'format_adapter_missing'})
            else:
                others[extension(member)] += 1
    return tables, others, skipped


def profile_artifact(root, artifact, options, limits, budget_tables):
    """Tables, counted other files and skipped files for one held artifact (a file or an archive)."""
    rel = artifact['path']
    path = root / rel
    record = {'path': rel, 'bytes': artifact.get('bytes'), 'sha256': artifact.get('sha256'), 'tables': [], 'others': {}, 'skipped': []}
    if not path.is_file():
        record['skipped'].append({'reason': 'file_missing_on_disk'})
        return record
    if artifact.get('bytes') is not None and path.stat().st_size != artifact['bytes']:
        record['skipped'].append({'reason': 'size_differs_from_receipt'})
        return record
    kind = file_kind(path.name)
    try:
        if kind == 'table':
            with path.open('rb') as binary, text_stream(binary, path.name) as stream:
                result = profile_csv(stream, path.name, options)
            record['tables'] = [{'member': None, 'bytes': artifact.get('bytes'), **result}] if result['state'] == 'profiled' else []
            if result['state'] != 'profiled':
                record['skipped'].append({'reason': result['reason']})
        elif kind == 'arff':
            with path.open('rb') as binary, text_stream(binary, path.name) as stream:
                result = profile_arff(stream, options)
            record['tables'] = [{'member': None, 'bytes': artifact.get('bytes'), **result}] if result['state'] == 'profiled' else []
            if result['state'] != 'profiled':
                record['skipped'].append({'reason': result['reason']})
        elif kind == 'sqlite':
            temporary = None
            try:
                target = path
                if path.name.lower().endswith('.gz'):
                    temporary = target = decompress_to_temp(path, limits['max_decompressed_bytes_per_file'])
                if target is None:
                    record['skipped'].append({'reason': 'decompression_budget'})
                else:
                    record['tables'] = profile_sqlite(target, {**options, 'max_tables': budget_tables})
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        elif kind == 'ndjson':
            with path.open('rb') as binary, text_stream(binary, path.name) as stream:
                result = profile_records(stream, options, 'ndjson')
            record['tables'] = [{'member': None, 'bytes': artifact.get('bytes'), **result}] if result['state'] == 'profiled' else []
            if result['state'] != 'profiled':
                record['skipped'].append({'reason': result['reason']})
        elif kind == 'json':
            with path.open('rb') as binary:
                result = profile_json_file(binary, path.name, path.stat().st_size, options, limits)
            if result['state'] == 'profiled':
                record['tables'] = [{'member': None, 'bytes': artifact.get('bytes'), **result}]
            elif result['state'] == 'document':
                record['others'] = {'json document': 1}
            else:
                record['skipped'].append({'reason': result['reason']})
        elif kind == 'zip':
            tables, others, skipped = profile_zip(path, options, limits, budget_tables)
            record.update(tables=tables, others=dict(others), skipped=skipped)
        elif kind == 'adapter_missing':
            record['skipped'].append({'reason': 'format_adapter_missing'})
        else:
            record['others'] = {extension(path.name): 1}
    except (UnicodeError, csv.Error, ValueError, OSError, zipfile.BadZipFile, sqlite3.Error) as error:
        record['skipped'].append({'reason': 'parse_failed: ' + type(error).__name__})
    return record


DISK_STATE_REASONS = {'file_missing_on_disk', 'size_differs_from_receipt'}


def code_identity():
    return digest(Path(__file__))


def census_records(root):
    """The latest census record of this study and of the study workspaces beside it (read-only); this study's first."""
    neighbours = sorted(p for p in root.parent.iterdir() if p.is_dir() and p.resolve() != root and (p / 'study.json').is_file()) if root.parent.is_dir() else []
    records = []
    for folder in [root] + neighbours:
        try:
            pointer = read(folder / 'data/interim/dataset_structure/latest.json')
            records.append((folder, read(folder / pointer['structure'])))
        except (OSError, ValueError, KeyError):
            continue
    return records


def previous_profiles(root, options, limits):
    """Artifact profiles of earlier censuses, this study's and its neighbours', keyed by hash and file name.

    A profile is a function of the bytes, the file name, the options, the limits and this code, so one computed by another
    study for the same bytes is the same profile: a merged or reusing study need not profile copied files again.
    """
    profiles = {}
    for folder, record in census_records(root):
        if record.get('code_sha256') != code_identity() or record.get('options') != options or record.get('limits') != limits:
            continue
        for d in record.get('datasets', []):
            if not d.get('profiled'):
                continue
            for a in d.get('artifacts', []):
                if a.get('sha256') and not any(x.get('reason') in DISK_STATE_REASONS for x in a.get('skipped', [])):
                    profiles.setdefault((a['sha256'], a['path'].rsplit('/', 1)[-1]), (a, folder == root))
    return profiles


def collect(root, options=None, only=None, reuse=True):
    root = Path(root).resolve()
    options = {**DEFAULTS, **(options or {})}
    receipt = read(root / 'data/interim/dataset_selection/latest.json')
    catalogue = read(root / receipt['selection'])
    validation = read(root / 'configs/dataset_validation.json') if (root / 'configs/dataset_validation.json').exists() else {}
    limits = {**DEFAULT_LIMITS, **validation.get('limits', {})}
    families = validation.get('families', {})
    lock_path = root / 'configs/research_resources.lock.json'
    lock_files = len(read(lock_path)['files']) if lock_path.exists() else 0
    licences = read(root / 'configs/data_licenses.json')['resources'] if (root / 'configs/data_licenses.json').exists() else []
    reusable = previous_profiles(root, options, limits) if reuse else {}
    datasets, cache, reused, reused_elsewhere = [], {}, 0, 0
    for d in catalogue['datasets']:
        artifacts = d.get('obtained_artifacts', [])
        entry = {'id': d['id'], 'name': d['name'], 'category': d.get('category'), 'access_route': d.get('access_route', ''), 'registration_url': d.get('registration_url'),
                 'scope': d.get('scope', ''), 'source_urls': d.get('source_urls', []), 'completeness': d.get('completeness'), 'held_files': len(artifacts),
                 'held_bytes': sum(a.get('bytes') or 0 for a in artifacts) if artifacts else None, 'locators': d.get('source_locators', []),
                 'next_action': d.get('next_action', ''), 'preaccess': d.get('preaccess_screen') or {}, 'artifacts': [], 'profiled': bool(artifacts)}
        if artifacts and (only is None or d['id'] in only):
            print(f"profiling {d['id']} ({len(artifacts)} files)", file=sys.stderr)
            tables_so_far = 0
            for artifact in artifacts:
                if artifact['path'] not in cache:
                    previous = reusable.get((artifact.get('sha256'), artifact['path'].rsplit('/', 1)[-1]))
                    present = (root / artifact['path']).is_file() and (artifact.get('bytes') is None or (root / artifact['path']).stat().st_size == artifact['bytes'])
                    if previous is not None and present:  # same bytes, name, options and code: the profile cannot differ
                        record, own = previous
                        cache[artifact['path']] = {**deepcopy(record), 'path': artifact['path']}
                        reused += 1
                        reused_elsewhere += 0 if own else 1
                    else:
                        cache[artifact['path']] = profile_artifact(root, artifact, options, limits, max(options['max_tables'] - tables_so_far, 0))
                record = cache[artifact['path']]
                tables_so_far += len(record['tables'])
                entry['artifacts'].append(record)
        elif artifacts:
            entry['profiled'] = False
        entry['tables'] = sum(len(a['tables']) for a in entry['artifacts'])
        entry['rows'] = sum(t['rows'] for a in entry['artifacts'] for t in a['tables'])
        entry['columns'] = sum(len(t['columns']) for a in entry['artifacts'] for t in a['tables'])
        others = Counter()
        for a in entry['artifacts']:
            others.update(a['others'])
        entry['others'] = dict(others.most_common())
        entry['skipped'] = [{'path': a['path'], **s} for a in entry['artifacts'] for s in a['skipped']]
        datasets.append(entry)
    held_ids = {d['id'] for d in datasets if d['held_files']}
    for d in datasets:
        d['family_held'] = sorted({member for name, members in families.items() if d['id'] in members for member in members if member in held_ids and member != d['id']})
        d['documentation'] = sorted({e['snapshot'] for r in licences if d['id'] in r.get('catalog_ids', []) for e in r.get('evidence', []) if e.get('snapshot')}
                                    | {e['path'] for e in (d['preaccess'].get('evidence') or []) if isinstance(e, dict) and e.get('path')})
        d['terms_sources'] = sorted({u for r in licences if d['id'] in r.get('catalog_ids', []) for u in r.get('sources', [])} | set(d['preaccess'].get('sources') or []))
    return {'root': root, 'receipt': receipt, 'datasets': datasets, 'lock_files': lock_files, 'receipt_files': receipt.get('summary', {}).get('rechecked_unique_artifacts'),
            'options': options, 'limits': limits, 'reused_artifacts': reused, 'reused_from_other_studies': reused_elsewhere}


def column_row(column):
    kind = column['type'] if not column.get('declared_type') else f"{column['declared_type']} → {column['type']}"
    distinct = fmt_num(column['distinct']) if column['distinct'] is not None else (f"> {fmt_num(column['distinct_at_least'])}" if column.get('distinct_at_least') else '—')
    if column.get('key_like'):
        distinct += ' (unique)'
    parts = []
    numeric = column.get('numeric')
    if numeric:
        median = ('≈ ' if numeric['median_is_estimate'] else '') + fmt_num(numeric['median'])
        parts.append(f"min {fmt_num(numeric['min'])} · median {median} · mean {fmt_num(round(numeric['mean'], 4))} · max {fmt_num(numeric['max'])}")
    if column.get('time_range'):
        parts.append(f"{column['time_range'][0]} … {column['time_range'][1]}")
    if column.get('labels'):
        parts.append('labels: ' + ', '.join(f"{cell(l['value'], 32)} ({fmt_num(l['count'])})" for l in column['labels'][:8]) + (' …' if len(column['labels']) > 8 else ''))
    elif column.get('text_length') and not column.get('key_like'):
        parts.append(f"text length {column['text_length'][0]}–{column['text_length'][1]}")
    values = fmt_num(column['values']) if column['values'] is not None else '—'
    missing = '—' if column['missing'] is None else (f"{column['missing'] / (column['values'] + column['missing']):.1%}" if column['values'] + column['missing'] else '—')
    return f"| `{cell(column['name'], 40)}` | {cell(kind, 40)} | {values} | {missing} | {distinct} | {' · '.join(parts) or '—'} |"


def table_heading(table):
    rows = ('≥ ' if table.get('rows_are_lower_bound') else '') + fmt_num(table['rows'])
    note = ''
    if table.get('sparse'):
        note = ' · sparse ARFF: declarations only'
    elif table['profiled_rows'] < table['rows']:
        note = f" · statistics from the first {fmt_num(table['profiled_rows'])} rows"
    if table.get('ragged_rows'):
        note += f" · {fmt_num(table['ragged_rows'])} rows with a different width"
    if table.get('resource_types'):
        note += ' · resource types: ' + ', '.join(f'{k} ({v})' for k, v in table['resource_types'].items())
    return f"{rows} rows × {len(table['columns'])} columns · {table['format']}{note}"


def render_dataset(d, options):
    out = [f"### {d['id']} — {cell(d['name'], 120)}", '', f"<a id=\"{d['id'].lower()}\"></a>", '']
    where = ', '.join(f'`{loc}`' for loc in d['locators']) or 'see the dataset page'
    others = ', '.join(f'{n} {ext}' for ext, n in d['others'].items()) or 'none'
    out.append(f"Held: {fmt_num(d['held_files'])} verified files ({fmt_bytes(d['held_bytes'])}) under {where}. Tables profiled: {fmt_num(d['tables'])} "
               f"({fmt_num(d['rows'])} rows, {fmt_num(d['columns'])} columns in total). Other files: {others}. "
               f"[Dataset page](DATASET_SELECTION.md#{d['id'].lower()}).")
    out.append('')
    if not d['profiled']:
        out += ['Not profiled in this run (restricted to other datasets); the counts above come from the receipt only.', '']
        return out
    if d['skipped']:
        reasons = Counter(s['reason'] for s in d['skipped'])
        out.append('Not profiled: ' + '; '.join(f"{n} file(s): {r.replace('_', ' ')}" for r, n in reasons.most_common()) + '.')
        out.append('')
    tables = [(a['path'], t) for a in d['artifacts'] for t in a['tables']]
    if not tables:
        out += ['No tabular file was found among the held files: the release is code, documentation, archives without tables or a format without an adapter.', '']
        return out
    out += ['<details>', f"<summary>{fmt_num(len(tables))} tables, column by column</summary>", '']
    for number, (path, table) in enumerate(tables[:options['rendered_tables']], 1):
        name = path + ((' → ' + table['member']) if table.get('member') else '')
        out += [f"#### {d['id']} · table {number} · `{cell(name, 140)}`", '', table_heading(table), '', '| Column | Type | Values | Missing | Distinct | Summary |', '|---|---|---|---|---|---|']
        out += [column_row(c) for c in table['columns'][:options['rendered_columns']]]
        if len(table['columns']) > options['rendered_columns']:
            out.append(f"| … | | | | | {fmt_num(len(table['columns']) - options['rendered_columns'])} more columns in the JSON record |")
        out.append('')
    if len(tables) > options['rendered_tables']:
        out += [f"{fmt_num(len(tables) - options['rendered_tables'])} more tables, listed without columns:", '', '| Table | Rows | Columns | Format |', '|---|---|---|---|']
        for path, table in tables[options['rendered_tables']:]:
            name = path + ((' → ' + table['member']) if table.get('member') else '')
            out.append(f"| `{cell(name, 120)}` | {('≥ ' if table.get('rows_are_lower_bound') else '') + fmt_num(table['rows'])} | {fmt_num(len(table['columns']))} | {table['format']} |")
        out.append('')
    out += ['</details>', '']
    return out


def render_not_held(d, by_id):
    out = [f"### {d['id']} — {cell(d['name'], 120)}", '', f"<a id=\"{d['id'].lower()}\"></a>", '']
    route = d['access_route'].replace('_', ' ') or 'unknown'
    out.append(f"- **Access route:** {route}" + (f" — [register or request]({d['registration_url']})" if d.get('registration_url') else '') + '.')
    if d['scope']:
        out.append(f"- **What the public record says it contains:** {cell(d['scope'], 600)}")
    if d['family_held']:
        proxies = ', '.join(f"[{i}](#{i.lower()}) ({fmt_num(by_id[i]['tables'])} tables)" for i in d['family_held'])
        out.append(f"- **Same-family releases held in this study:** {proxies}. A provider's demo or derived release shows the table and column layout you can expect; "
                   'its row counts, value ranges and version are its own, not the gated release’s.')
    else:
        out.append('- **Same-family releases held in this study:** none registered. If the provider publishes a demo subset, register it as its own catalogue entry: a held demo is the most reliable preview of the structure.')
    if d['documentation']:
        out.append('- **Retained public documentation:** ' + ', '.join(f"[{Path(p).name}](<../{p}>)" for p in d['documentation'][:8]) + (' …' if len(d['documentation']) > 8 else '') + '.')
    if d['terms_sources'] or d['source_urls']:
        links = [f'[provider {i}]({u})' for i, u in enumerate(d['source_urls'], 1)] + [f'[terms {i}]({u})' for i, u in enumerate(d['terms_sources'], 1)]
        out.append('- **Where the provider documents the structure:** ' + ' · '.join(links) + '.')
    screen = d['preaccess']
    if screen.get('summary'):
        out.append(f"- **Terms screen:** {cell(screen.get('status', ''), 60).replace('_', ' ')}: {cell(screen['summary'], 400)}")
    out.append('- **Unknown until access:** the exact table list, column names, row counts and sizes of this release are not in the local evidence'
               + (', beyond what the same-family releases above indicate' if d['family_held'] else '') + '. Nothing here grants access or AI use.')
    out.append('')
    return out


def render(data):
    root, datasets, options = data['root'], data['datasets'], data['options']
    study_id = workspace_study_id(root) or default_study(root)
    held = [d for d in datasets if d['held_files']]
    not_held = [d for d in datasets if not d['held_files']]
    by_id = {d['id']: d for d in datasets}
    out = ['# Data structure: tables, columns, types and statistics', '',
           f"Study `{study_id}`. Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from the catalogue receipt `{data['receipt']['selection_sha256'][:16]}…` "
           f"({fmt_num(len(held))} held datasets, {fmt_num(sum(d['held_files'] for d in held))} verified files; {fmt_num(len(not_held))} datasets not held). "
           'Deterministic and offline: no model calls, no network. Statistics are aggregates over the held files; no cell values are copied except the labels of columns with '
           f"at most {options['label_levels']} distinct values. Column statistics use the first {fmt_num(options['max_rows'])} rows of a table; row counts are complete unless marked ≥.", '',
           'This document grants no data or AI-use permission. Column names, labels and statistics are a dictionary-like derivative of the sources: before a model reads them, '
           'the same source-bound preflight applies as to the files themselves (see [Licensing](DATA_LICENSES_AND_AI_USE.md)).', '']
    if data.get('reused_artifacts'):
        out += [f"{fmt_num(data['reused_artifacts'])} file profiles were reused from the previous census"
                + (f" ({fmt_num(data['reused_from_other_studies'])} of them from other studies' censuses of the same bytes)" if data.get('reused_from_other_studies') else '')
                + ': same bytes, options and profiling code, so the result cannot differ.', '']
    if data['receipt_files'] is not None and data['lock_files'] and data['lock_files'] != data['receipt_files']:
        out += [f"**The catalogue receipt is older than the holdings:** the acquisition lock lists {fmt_num(data['lock_files'])} files but the receipt knows {fmt_num(data['receipt_files'])}. "
                'Run `Scripts/validate_dataset_workspace.py` and `Scripts/document_dataset_selection.py`, then this report again, so every acquired file is profiled.', '']
    out += ['## At a glance', '', '| Dataset | Held files | Tables | Rows | Columns | Other files | Not profiled |', '|---|---|---|---|---|---|---|']
    for d in sorted(held, key=lambda x: x['id']):
        others = sum(d['others'].values())
        out.append(f"| [{d['id']}](#{d['id'].lower()}) | {fmt_num(d['held_files'])} ({fmt_bytes(d['held_bytes'])}) | {fmt_num(d['tables'])} | {fmt_num(d['rows'])} | {fmt_num(d['columns'])} | {fmt_num(others)} | {fmt_num(len(d['skipped']))} |")
    out += ['', f"Not held ({fmt_num(len(not_held))}): " + ', '.join(f"[{d['id']}](#{d['id'].lower()})" for d in sorted(not_held, key=lambda x: x['id'])) + '. See [Before you apply](#before-you-apply-what-is-known-about-the-datasets-that-are-not-held).' if not_held else '', '']
    out += ['## How to read the tables', '',
            '- **Type** is inferred from the values (integer, number, date, datetime, boolean, text; mixed when kinds disagree) or, for ARFF and SQLite, the declared type followed by the inferred one.',
            '- **Values** and **Missing** count non-missing cells and the share of missing cells (empty, NA, NULL, ?). **Distinct** counts different values; (unique) marks a column whose values never repeat, a candidate key.',
            '- **Summary** gives min, median, mean and max for numbers (≈ marks a median estimated from a sample), the first and last timestamp for dates, the labels and counts of low-cardinality columns, and the text length range otherwise.',
            '- Files inside ZIP archives are profiled when they are tables within the parse budget; code, documentation, images and formats without an adapter are only counted.', '']
    if held:
        out += ['## Held datasets', '']
        for d in sorted(held, key=lambda x: x['id']):
            out += render_dataset(d, options)
    if not_held:
        out += ['## Before you apply: what is known about the datasets that are not held', '',
                'For a registration-gated release nothing can be profiled before access. What the local evidence does give: the access route, the public scope statement retained at intake, '
                'the held releases of the same registry family (a provider demo shows the table and column layout), the retained notices and the provider pages that document the structure.', '']
        for d in sorted(not_held, key=lambda x: x['id']):
            out += render_not_held(d, by_id)
    return '\n'.join(out)


def write_document(root=ROOT, options=None, only=None, reuse=True):
    root = Path(root).resolve()
    data = collect(root, options, only, reuse)
    text = render(data)
    doc = root / 'docs/DATASET_STRUCTURE.md'
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = root / 'data/interim/dataset_structure' / stamp
    folder.mkdir(parents=True)
    record = {'schema_version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(), 'catalogue_sha256': data['receipt']['selection_sha256'], 'options': data['options'],
              'limits': data['limits'], 'lock_files': data['lock_files'], 'receipt_files': data['receipt_files'], 'model_calls': 0,
              'code_sha256': code_identity(), 'reused_artifacts': data['reused_artifacts'], 'reused_from_other_studies': data['reused_from_other_studies'],
              'datasets': [{k: v for k, v in d.items() if k != 'preaccess'} for d in data['datasets']]}
    structure = folder / 'structure.json'
    structure.write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str) + '\n')
    (root / 'data/interim/dataset_structure/latest.json').write_text(json.dumps(
        {'run': stamp, 'structure': str(structure.relative_to(root)), 'structure_sha256': digest(structure), 'document': 'docs/DATASET_STRUCTURE.md', 'document_sha256': digest(doc),
         'summary': {'datasets': len(data['datasets']), 'held': sum(1 for d in data['datasets'] if d['held_files']), 'tables': sum(d['tables'] for d in data['datasets']),
                     'rows': sum(d['rows'] for d in data['datasets']), 'columns': sum(d['columns'] for d in data['datasets']), 'not_profiled': sum(len(d['skipped']) for d in data['datasets'])}},
        indent=2) + '\n')
    return doc, data


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--datasets', help='Comma-separated catalogue IDs to profile; the others are listed from the receipt only')
    parser.add_argument('--max-rows', type=int, default=DEFAULTS['max_rows'], help='Rows per table used for the column statistics (default %(default)s)')
    parser.add_argument('--max-tables', type=int, default=DEFAULTS['max_tables'], help='Tables profiled per dataset (default %(default)s)')
    parser.add_argument('--report', action='store_true', help='Also save a dated tracked copy under reports/ and register it with the study')
    parser.add_argument('--no-reuse', action='store_true', help='Profile every held file again instead of reusing this or a neighbouring study’s census for files whose bytes, options and code are unchanged')
    args = parser.parse_args(argv)
    only = {x.strip() for x in args.datasets.split(',') if x.strip()} if args.datasets else None
    doc, data = write_document(root, {'max_rows': args.max_rows, 'max_tables': args.max_tables}, only, reuse=not args.no_reuse)
    result = {'document': str(doc), 'datasets': len(data['datasets']), 'held': sum(1 for d in data['datasets'] if d['held_files']),
              'tables': sum(d['tables'] for d in data['datasets']), 'not_profiled_files': sum(len(d['skipped']) for d in data['datasets']), 'reused_artifacts': data['reused_artifacts'],
              'reused_from_other_studies': data['reused_from_other_studies']}
    if args.report:
        from report_dataset_status import save_report
        result['report'] = str(save_report(root, doc, title='Data structure: tables, columns, types and statistics', stem='dataset-structure', runs_dir='structure_reports'))
    print(json.dumps(result))
    return result


if __name__ == '__main__':
    require_workspace(ROOT)
    main()
