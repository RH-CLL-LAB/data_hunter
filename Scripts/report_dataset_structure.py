#!/usr/bin/env python3
"""Write the data-structure report: tables, columns, types and descriptive statistics of the held files, and what is known
before access for the datasets that are not held.

Reads the current catalogue receipt (which verified, locked files each dataset holds), the validation registry (families,
size limits) and the licence registers (retained documentation), and writes docs/DATASET_STRUCTURE.md plus a JSON record
under data/interim/dataset_structure/<run>/:

  1. held datasets, one section each: their composition (files, size, tables and formats per folder), then every tabular
     file or archive member (CSV/TSV, delimited text in .txt/.psv/.dat files, gzip-compressed tables, ARFF, SQLite, NDJSON,
     record-list JSON, Excel workbooks sheet by sheet, Parquet schemas with their footer statistics, SAS transport (XPORT v5)
     datasets with variable labels, tables inside ZIP archives) as a table with its columns, declared or inferred types,
     missingness, distinct counts, numeric and date ranges and the labels of low-cardinality columns; tables with the same
     columns (per-patient files, yearly extracts) are combined into one entry; the other files are counted by extension,
     archive metadata (macOS __MACOSX/ resource forks, .DS_Store) is ignored, and files no reader handles are listed with
     the reason;
  2. datasets that are not held (registration, agreement or licence decision pending, or an open release not yet acquired):
     the access route, the registry's scope statement, the same-family releases that ARE held (a provider's demo release
     shows the table and column layout of the gated release, not its counts), the retained public documentation, and what
     stays unknown until access.

Deterministic, offline, no model calls, standard library only. Statistics are aggregates: no cell values are copied except
the labels of columns with few distinct values. The document grants no data or AI-use permission; as a dictionary-like
derivative of the sources it is covered by the same source-bound preflight as the files it describes before any model
reads it. A variable dictionary, docs/DATASET_VARIABLES.csv, lists every column of every profiled table once (tables with
the same columns as one entry) for filtering in a spreadsheet. `--report` also saves dated tracked copies of both under
reports/ and registers the report with the study.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
import gzip
from hashlib import sha256
import io
from itertools import chain
import json
import math
from pathlib import Path
import posixpath
import random
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
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
TEXT_TABLE_SUFFIXES = ('.txt', '.psv', '.dat', '.tab', '.txt.gz', '.psv.gz', '.dat.gz')  # delimited text behind a generic name: read only when every sampled line splits alike
NOT_TABLE_STEMS = ('readme', 'license', 'licence', 'notice', 'changelog', 'changes', 'copying', 'authors', 'citation', 'requirements', 'news', 'install', 'contributing',
                   'manifest', 'sha256sums', 'md5sums', 'robots')
ADAPTER_MISSING = ('.feather', '.xls', '.sav', '.dta', '.rds', '.rdata', '.sas7bdat', '.h5', '.hdf5', '.mat', '.npy', '.npz', '.pkl', '.pickle', '.pt')
ARCHIVE_METADATA = 'archive metadata'  # macOS resource forks and folder settings that zip tools add: not part of the data
METADATA_NAMES = {'.ds_store', 'thumbs.db', 'desktop.ini'}
RANDOM_ACCESS_KINDS = {'json', 'xlsx', 'parquet', 'xpt'}  # read whole or by seeking; the other table kinds are streamed
TABLE_KINDS = {'table', 'text_table', 'arff', 'ndjson'} | RANDOM_ACCESS_KINDS
BUDGET_REASONS = {'table_budget_for_dataset', 'archive_member_budget'}  # a profile cut by a budget depends on its context and is never reused
DEFAULT_LIMITS = {'max_decompressed_bytes_per_file': 512 * 1024 * 1024, 'max_archive_members': 100000, 'max_member_bytes_to_parse': 16 * 1024 * 1024}
DEFAULTS = {'max_rows': 200000, 'max_count_rows': 5000000, 'max_tables': 100000, 'distinct_cap': 50000, 'top_cap': 200, 'reservoir': 2000, 'label_levels': 50, 'rendered_tables': 60, 'rendered_columns': 60,
            'rendered_labels': 20}
PROFILE_OPTIONS = ('max_rows', 'max_count_rows', 'distinct_cap', 'top_cap', 'reservoir', 'label_levels')  # what a file's profile depends on; the table budget only cuts, and a cut is recorded
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


def sniff_delimiter(lines):
    """The delimiter that splits every sampled line into the same number (at least two) of fields, or None: prose is not a table."""
    for delimiter in ('\t', ',', '|', ';'):
        widths = {len(row) for row in csv.reader(lines, delimiter=delimiter)}
        if len(widths) == 1 and widths.pop() >= 2:
            return delimiter
    return None


def profile_text_table(stream, name, options):
    """A .txt/.psv/.dat file that is a delimited table (a header and at least two rows that split alike); anything else is counted as text."""
    lines = []
    for line in stream:
        if line.strip():
            lines.append(line)
            if len(lines) >= 6:
                break
    if len(lines) < 3 or '\x00' in lines[0]:
        return {'state': 'other'}
    delimiter = sniff_delimiter(lines)
    if delimiter is None:
        return {'state': 'other'}
    header = next(csv.reader([lines[0]], delimiter=delimiter))
    if any(len(h) > 128 for h in header):
        return {'state': 'other'}
    body = csv.reader(chain(lines[1:], stream), delimiter=delimiter)
    profile = profile_rows(header, (row for row in body if row), options)
    return {'state': 'profiled', 'format': 'delimited text', 'delimiter': {'\t': 'tab', '|': 'pipe'}.get(delimiter, delimiter), **profile}


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
        if len(names) > options['max_tables']:
            tables.append({'state': 'skipped', 'reason': 'table_budget_for_dataset'})
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


XLSX_MAIN = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
XLSX_REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
XLSX_DATE_FORMATS = set(range(14, 23)) | {45, 46, 47}  # Excel's built-in date and time number formats


def column_index(reference):
    letters = re.match(r'[A-Z]+', reference or '')
    index = 0
    for letter in letters.group(0) if letters else '':
        index = index * 26 + ord(letter) - 64
    return index - 1


def excel_date(serial):
    """An Excel date serial as ISO text (1900 date system); None outside the representable range."""
    if not 0 < serial < 2958466:
        return None
    moment = datetime(1899, 12, 30) + timedelta(days=serial)
    return moment.date().isoformat() if serial == int(serial) else moment.strftime('%Y-%m-%d %H:%M:%S')


def xlsx_part(book, name, limits):
    info = book.getinfo(name)
    if info.file_size > limits['max_decompressed_bytes_per_file']:
        raise ValueError('workbook_part_over_parse_budget')
    return book.open(info)


def xlsx_date_styles(book, limits):
    """Indexes of the cell styles whose number format is a date or date-time."""
    if 'xl/styles.xml' not in book.namelist():
        return set()
    root = ET.parse(xlsx_part(book, 'xl/styles.xml', limits)).getroot()
    custom = {}
    for fmt in root.iter(XLSX_MAIN + 'numFmt'):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', '', fmt.get('formatCode', '')).lower()
        custom[int(fmt.get('numFmtId', -1))] = any(ch in code for ch in 'dy')
    cell_formats = root.find(XLSX_MAIN + 'cellXfs')
    return {i for i, xf in enumerate(cell_formats if cell_formats is not None else []) if int(xf.get('numFmtId', 0)) in XLSX_DATE_FORMATS or custom.get(int(xf.get('numFmtId', 0)))}


def xlsx_rows(stream, shared, dates):
    """Rows of one worksheet as lists of cell values in column order; empty cells are None, error cells missing."""
    row = {}
    for _, element in ET.iterparse(stream, events=('end',)):
        if element.tag == XLSX_MAIN + 'c':
            kind, style = element.get('t'), element.get('s')
            raw = element.find(XLSX_MAIN + 'v')
            text = raw.text if raw is not None else None
            if kind == 's' and text is not None:
                value = shared[int(text)] if int(text) < len(shared) else None
            elif kind == 'inlineStr':
                value = ''.join(t.text or '' for t in element.iter(XLSX_MAIN + 't'))
            elif kind == 'b':
                value = text == '1'
            elif kind == 'e':
                value = None
            elif kind in ('str', 'd'):
                value = text
            elif text is not None and style is not None and int(style) in dates:
                try:
                    value = excel_date(float(text)) or text
                except ValueError:
                    value = text
            else:
                value = text
            row[column_index(element.get('r')) if element.get('r') else len(row)] = value
            element.clear()
        elif element.tag == XLSX_MAIN + 'row':
            yield [row.get(i) for i in range(max(row) + 1)] if row else []
            row = {}
            element.clear()


def xlsx_tables(source, options, limits, budget_tables):
    """Every sheet of a workbook with data as a table: the first non-empty row is the header."""
    tables = []
    with zipfile.ZipFile(source) as book:
        names = set(book.namelist())
        if 'xl/workbook.xml' not in names:
            return [], 'not_an_excel_workbook'
        shared = []
        if 'xl/sharedStrings.xml' in names:
            for _, element in ET.iterparse(xlsx_part(book, 'xl/sharedStrings.xml', limits)):
                if element.tag == XLSX_MAIN + 'si':
                    shared.append(''.join(t.text or '' for t in element.iter(XLSX_MAIN + 't')))
                    element.clear()
        targets = {}
        if 'xl/_rels/workbook.xml.rels' in names:
            for relation in ET.parse(xlsx_part(book, 'xl/_rels/workbook.xml.rels', limits)).getroot():
                targets[relation.get('Id')] = relation.get('Target', '')
        dates = xlsx_date_styles(book, limits)
        for sheet in ET.parse(xlsx_part(book, 'xl/workbook.xml', limits)).getroot().iter(XLSX_MAIN + 'sheet'):
            if len(tables) >= budget_tables:
                return tables, 'table_budget_for_dataset'
            target = targets.get(sheet.get(XLSX_REL + 'id'), '')
            part = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
            if part not in names:
                continue
            rows = xlsx_rows(xlsx_part(book, part, limits), shared, dates)
            header = next((r for r in rows if any(v not in (None, '') for v in r)), None)
            if header is None:
                continue
            columns = [str(v).strip() if v not in (None, '') else f'column {i + 1}' for i, v in enumerate(header)]
            body = (r + [None] * (len(columns) - len(r)) for r in rows if any(v not in (None, '') for v in r))
            tables.append({'state': 'profiled', 'format': 'Excel sheet', 'sheet': sheet.get('name'), **profile_rows(columns, body, options)})
    return tables, (None if tables else 'no_sheet_with_data')


class Compact:
    """A minimal reader of the Thrift compact protocol: enough for the metadata footer of a Parquet file."""

    def __init__(self, data):
        self.data, self.position = data, 0

    def byte(self):
        value = self.data[self.position]
        self.position += 1
        return value

    def varint(self):
        shift = result = 0
        while True:
            value = self.byte()
            result |= (value & 0x7f) << shift
            if not value & 0x80:
                return result
            shift += 7

    def zigzag(self):
        value = self.varint()
        return (value >> 1) ^ -(value & 1)

    def value(self, kind):
        if kind in (1, 2):
            return kind == 1
        if kind == 3:
            value = self.byte()
            return value - 256 if value > 127 else value
        if kind in (4, 5, 6):
            return self.zigzag()
        if kind == 7:
            value = struct.unpack('<d', self.data[self.position:self.position + 8])[0]
            self.position += 8
            return value
        if kind == 8:
            size = self.varint()
            value = self.data[self.position:self.position + size]
            self.position += size
            return value
        if kind in (9, 10):
            header = self.byte()
            size, element = header >> 4, header & 0x0f
            size = self.varint() if size == 15 else size
            return [self.byte() == 1 if element in (1, 2) else self.value(element) for _ in range(size)]
        if kind == 11:
            size = self.varint()
            kinds = self.byte() if size else 0
            return {self.value(kinds >> 4): self.value(kinds & 0x0f) for _ in range(size)}
        if kind == 12:
            return self.struct()
        raise ValueError('unknown_thrift_type')

    def struct(self):
        fields, last = {}, 0
        while True:
            header = self.byte()
            if header == 0:
                return fields
            delta, kind = header >> 4, header & 0x0f
            last = last + delta if delta else self.zigzag()
            fields[last] = self.value(kind)


PARQUET_PHYSICAL = {0: 'BOOLEAN', 1: 'INT32', 2: 'INT64', 3: 'INT96', 4: 'FLOAT', 5: 'DOUBLE', 6: 'BYTE_ARRAY', 7: 'FIXED_LEN_BYTE_ARRAY'}
PARQUET_CONVERTED = {0: 'UTF8', 4: 'ENUM', 5: 'DECIMAL', 6: 'DATE', 7: 'TIME_MILLIS', 8: 'TIME_MICROS', 9: 'TIMESTAMP_MILLIS', 10: 'TIMESTAMP_MICROS', 19: 'JSON'}
PARQUET_LOGICAL = {1: 'STRING', 2: 'MAP', 3: 'LIST', 4: 'ENUM', 5: 'DECIMAL', 6: 'DATE', 7: 'TIME', 8: 'TIMESTAMP', 10: 'INTEGER', 11: 'NULL', 12: 'JSON', 13: 'BSON', 14: 'UUID', 15: 'FLOAT16'}


def parquet_leaves(schema):
    """Leaf columns of a Parquet schema (a depth-first list of elements) as (dotted path, element, repeated): a leaf under a
    repeated group (a list) counts list elements, not rows."""
    leaves, position = [], 1

    def walk(prefix, count, repeated):
        nonlocal position
        for _ in range(count):
            if position >= len(schema):
                return
            element = schema[position]
            position += 1
            name = element.get(4, b'').decode('utf-8', 'replace')
            inside = repeated or element.get(3) == 2
            if element.get(5):
                walk(prefix + [name], element[5], inside)
            else:
                leaves.append(('.'.join(prefix + [name]), element, inside))
    walk([], schema[0].get(5, len(schema) - 1) if schema else 0, False)
    return leaves


def parquet_type(element):
    """(declared type, inferred kind, timestamp unit) of a Parquet leaf column."""
    physical = PARQUET_PHYSICAL.get(element.get(1), 'UNKNOWN')
    logical = element.get(10) or {}
    name = next((PARQUET_LOGICAL[k] for k in logical if k in PARQUET_LOGICAL), None) or PARQUET_CONVERTED.get(element.get(6))
    unit = None
    if 8 in logical:
        unit = next(({1: 'ms', 2: 'us', 3: 'ns'}[k] for k in (logical[8].get(2) or {}) if k in (1, 2, 3)), None)
    elif element.get(6) in (9, 10):
        unit = 'ms' if element.get(6) == 9 else 'us'
    declared = physical + (f'/{name}' + (f'({unit})' if unit else '') if name else '')
    if name in ('STRING', 'UTF8', 'ENUM', 'JSON', 'UUID'):
        kind = 'text'
    elif name == 'DATE':
        kind = 'date'
    elif name in ('TIMESTAMP', 'TIMESTAMP_MILLIS', 'TIMESTAMP_MICROS') or physical == 'INT96':
        kind = 'datetime'
    elif name in ('TIME', 'TIME_MILLIS', 'TIME_MICROS'):
        kind = 'time'
    elif name == 'DECIMAL' or physical in ('FLOAT', 'DOUBLE'):
        kind = 'number'
    elif physical in ('INT32', 'INT64'):
        kind = 'integer'
    elif physical == 'BOOLEAN':
        kind = 'boolean'
    else:
        kind = 'binary'
    return declared, kind, unit, name == 'DECIMAL'


def parquet_statistic(raw, physical, kind, unit):
    """A footer min/max as a number or ISO time; None for text (cell values are not copied) and undecodable values."""
    formats = {('INT32', 4): '<i', ('INT64', 8): '<q', ('FLOAT', 4): '<f', ('DOUBLE', 8): '<d'}
    code = formats.get((physical, len(raw or b'')))
    if code is None:
        return None
    value = struct.unpack(code, raw)[0]
    try:
        if kind == 'date':
            return (datetime(1970, 1, 1) + timedelta(days=value)).date().isoformat()
        if kind == 'datetime' and unit:
            return (datetime(1970, 1, 1) + timedelta(microseconds=value * {'ms': 1000, 'us': 1, 'ns': 0.001}[unit])).strftime('%Y-%m-%d %H:%M:%S')
    except (OverflowError, ValueError):
        return None
    if kind in ('integer', 'number') and not (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return value
    return None


def parquet_table(handle, size, limits):
    """A Parquet file from its footer: schema, row count and the per-column statistics the writer stored (values are not read)."""
    if size < 12:
        return {'state': 'skipped', 'reason': 'not_a_parquet_file'}
    handle.seek(size - 8)
    tail = handle.read(8)
    if tail[4:] != b'PAR1':
        return {'state': 'skipped', 'reason': 'not_a_parquet_file'}
    length = struct.unpack('<I', tail[:4])[0]
    if length > size - 12 or length > limits['max_decompressed_bytes_per_file']:
        return {'state': 'skipped', 'reason': 'parquet_footer_over_parse_budget'}
    handle.seek(size - 8 - length)
    meta = Compact(handle.read(length)).struct()
    groups = meta.get(4, [])
    chunks = {}
    for group in groups:
        for chunk in group.get(1, []):
            info = chunk.get(3) or {}
            chunks.setdefault('.'.join(p.decode('utf-8', 'replace') for p in info.get(3, [])), []).append(info)
    columns = []
    for path, element, repeated in parquet_leaves(meta.get(2, [])):
        declared, kind, unit, decimal = parquet_type(element)
        declared += ' list element' if repeated else ''
        physical = PARQUET_PHYSICAL.get(element.get(1), 'UNKNOWN')
        infos = chunks.get(path, [])
        stats = [i.get(12) or {} for i in infos]
        nulls_known = bool(infos) and all(st.get(3) is not None for st in stats)
        nulls = sum(st.get(3, 0) for st in stats) if nulls_known else None
        listed = sum(i.get(5, 0) for i in infos) if infos else None
        column = {'name': path, 'declared_type': declared, 'type': kind, 'values': (listed - nulls) if nulls_known and listed is not None and not repeated else None,
                  'missing': None if repeated else nulls, 'distinct': (stats[0].get(4) if len(stats) == 1 and stats[0].get(4) is not None else None), 'distinct_at_least': None, 'key_like': False}
        if not decimal:
            lows = [v for v in (parquet_statistic(st.get(6, st.get(2)), physical, kind, unit) for st in stats) if v is not None]
            highs = [v for v in (parquet_statistic(st.get(5, st.get(1)), physical, kind, unit) for st in stats) if v is not None]
            if lows and highs and kind in ('integer', 'number'):
                column['numeric'] = {'min': min(lows), 'max': max(highs), 'mean': None, 'sd': None, 'median': None, 'median_is_estimate': False}
            elif lows and highs and kind in ('date', 'datetime'):
                column['time_range'] = [min(lows), max(highs)]
        columns.append(column)
    return {'state': 'profiled', 'format': 'Parquet', 'statistics': 'footer', 'rows': meta.get(3, 0), 'rows_are_lower_bound': False, 'profiled_rows': 0, 'ragged_rows': 0,
            'row_groups': len(groups), 'columns': columns}


XPT_LIBRARY = b'HEADER RECORD*******LIBRARY HEADER RECORD!!!!!!!'
XPT_LIBRARY_V8 = b'HEADER RECORD*******LIBV8   HEADER RECORD!!!!!!!'
XPT_MEMBER = b'HEADER RECORD*******MEMBER  HEADER RECORD!!!!!!!'
XPT_NAMESTR = b'HEADER RECORD*******NAMESTR HEADER RECORD!!!!!!!'
XPT_OBS = b'HEADER RECORD*******OBS     HEADER RECORD!!!!!!!'
SAS_DATETIME_FORMATS = ('DATETIME', 'E8601DT', 'B8601DT', 'IS8601DT', 'DATEAMPM')
SAS_DATE_FORMATS = ('DATE', 'YYMMDD', 'MMDDYY', 'DDMMYY', 'E8601DA', 'B8601DA', 'IS8601DA', 'WEEKDATE', 'WORDDATE', 'MONYY', 'YYMON', 'NLDATE')


def ibm_float(raw):
    """An IBM System/370 floating-point number as SAS transport stores it (2 to 8 bytes); None for SAS missing values."""
    if len(raw) < 2:
        return None
    first = raw[0]
    if not any(raw[1:]) and (first in (0x2e, 0x5f) or 0x41 <= first <= 0x5a):
        return None
    mantissa = int.from_bytes(raw[1:].ljust(7, b'\x00'), 'big')
    if not mantissa:
        return 0.0
    value = mantissa / float(1 << 56) * 16.0 ** ((first & 0x7f) - 64)
    return -value if first & 0x80 else value


def sas_value(value, sas_format):
    """A SAS numeric with a date or date-time format as ISO text; integral values as integers."""
    try:
        if sas_format.startswith(SAS_DATETIME_FORMATS):
            return (datetime(1960, 1, 1) + timedelta(seconds=value)).strftime('%Y-%m-%d %H:%M:%S')
        if sas_format.startswith(SAS_DATE_FORMATS):
            return (datetime(1960, 1, 1) + timedelta(days=value)).date().isoformat()
    except (OverflowError, ValueError):
        return value
    return int(value) if value == int(value) and abs(value) < 2 ** 53 else value


def xpt_tables(data, options):
    """Every dataset (member) of a SAS transport file (XPORT version 5) with its variables, labels and observations."""
    if data.startswith(XPT_LIBRARY_V8):
        return [], 'sas_transport_version_8_not_read'
    if not data.startswith(XPT_LIBRARY):
        head = data[:200].lstrip().lower()
        return [], ('content_is_an_html_page_not_sas_transport' if head.startswith((b'<html', b'<!doctype', b'<head')) else 'not_sas_transport')
    starts, position = [], data.find(XPT_MEMBER)
    while position != -1:
        if position % 80 == 0:
            starts.append(position)
        position = data.find(XPT_MEMBER, position + 1)
    tables = []
    for number, start in enumerate(starts):
        block = data[start:starts[number + 1] if number + 1 < len(starts) else len(data)]
        size = int(block[74:78] or b'140')
        name = block[168:176].decode('latin-1').strip()
        label = block[272:312].decode('latin-1').strip()
        if not block[320:400].startswith(XPT_NAMESTR):
            return tables, 'malformed_sas_transport'
        count = int(block[374:378])
        variables = []
        for k in range(count):
            raw = block[400 + k * size:400 + k * size + 140].ljust(140, b'\x00')
            ntype, _, length, _, vname, vlabel, vformat, _, _, _, _, _, _, _, offset, _ = struct.unpack('>hhhh8s40s8shhh2s8shhl52s', raw)
            variables.append({'name': vname.decode('latin-1').strip(), 'label': vlabel.decode('latin-1').strip(), 'format': vformat.decode('latin-1').strip().upper(),
                              'numeric': ntype == 1, 'length': length, 'offset': offset})
        observations = 400 + -(-count * size // 80) * 80
        if not block[observations:observations + 80].startswith(XPT_OBS):
            return tables, 'malformed_sas_transport'
        region = block[observations + 80:]
        width = max((v['offset'] + v['length'] for v in variables), default=0)
        total = len(region) // width if width else 0
        while total and (total - 1) * width >= len(region) - 80 and region[(total - 1) * width:total * width].strip(b' ') == b'':
            total -= 1  # the space padding of the last 80-byte record is not an observation

        def rows(region=region, variables=variables, width=width, total=total):
            for k in range(total):
                record = region[k * width:(k + 1) * width]
                values = []
                for v in variables:
                    raw = record[v['offset']:v['offset'] + v['length']]
                    if v['numeric']:
                        value = ibm_float(raw)
                        values.append(None if value is None else sas_value(value, v['format']))
                    else:
                        values.append(raw.decode('latin-1').rstrip())
                yield values
        declared = {v['name']: f"{'num' if v['numeric'] else 'char'}({v['length']})" + (f" {v['format']}" if v['format'] else '') for v in variables}
        profile = profile_rows([v['name'] for v in variables], rows(), options, declared)
        for column, v in zip(profile['columns'], variables):
            if v['label']:
                column['label'] = v['label']
        tables.append({'state': 'profiled', 'format': 'SAS transport (XPORT v5)', 'sheet': name, 'dataset_label': label or None, **profile})
    return tables, (None if tables else 'no_dataset_in_sas_transport')


def is_archive_metadata(name):
    parts = name.replace('\\', '/').split('/')
    return '__MACOSX' in parts or parts[-1].startswith('._') or parts[-1].lower() in METADATA_NAMES


def file_kind(name):
    lowered = name.lower()
    if is_archive_metadata(name):
        return 'metadata'
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
    if lowered.endswith(('.xlsx', '.xlsm')):
        return 'xlsx'
    if lowered.endswith('.parquet'):
        return 'parquet'
    if lowered.endswith('.xpt'):
        return 'xpt'
    if lowered.endswith(TEXT_TABLE_SUFFIXES) and not posixpath.basename(lowered).startswith(NOT_TABLE_STEMS):
        return 'text_table'
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


PARSE_ERRORS = (UnicodeError, csv.Error, ValueError, OSError, EOFError, zipfile.BadZipFile, sqlite3.Error, ET.ParseError, struct.error, KeyError, IndexError)


def profile_named(kind, open_binary, name, size, options, limits, budget_tables):
    """(tables, other-file label, skip reason) for one file or archive member of a table kind; `open_binary` opens it afresh.

    Multi-table formats (workbook sheets, SAS transport members) name each table in its 'sheet' field."""
    if kind in ('table', 'text_table', 'arff', 'ndjson'):
        with open_binary() as binary, text_stream(binary, name) as stream:
            result = (profile_csv(stream, name, options) if kind == 'table' else profile_text_table(stream, name, options) if kind == 'text_table'
                      else profile_arff(stream, options) if kind == 'arff' else profile_records(stream, options, 'ndjson'))
        if result['state'] == 'other':
            return [], extension(name), None
    elif kind == 'json':
        with open_binary() as binary:
            result = profile_json_file(binary, name, size, options, limits)
        if result['state'] == 'document':
            return [], 'json document', None
    elif kind == 'xlsx':
        if size > limits['max_decompressed_bytes_per_file']:
            return [], None, 'workbook_over_parse_budget'
        with open_binary() as binary:
            tables, reason = xlsx_tables(binary, options, limits, budget_tables)
        return tables, None, reason
    elif kind == 'parquet':
        with open_binary() as binary:
            result = parquet_table(binary, size, limits)
    elif kind == 'xpt':
        if size > limits['max_decompressed_bytes_per_file']:
            return [], None, 'transport_file_over_parse_budget'
        with open_binary() as binary:
            tables, reason = xpt_tables(binary.read(), options)
        return tables[:budget_tables], None, ('table_budget_for_dataset' if len(tables) > budget_tables else reason)
    else:
        raise ValueError('not_a_table_kind: ' + kind)
    return ([result], None, None) if result['state'] == 'profiled' else ([], None, result['reason'])


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
            if kind == 'metadata':
                others[ARCHIVE_METADATA] += 1
            elif kind in TABLE_KINDS:
                if len(tables) >= budget_tables:
                    skipped.append({'member': member, 'reason': 'table_budget_for_dataset'})
                    continue
                if kind in RANDOM_ACCESS_KINDS and info.file_size > limits['max_member_bytes_to_parse']:
                    skipped.append({'member': member, 'bytes': info.file_size, 'reason': 'member_over_parse_budget'})
                    continue  # read whole or by seeking; delimited tables are streamed whatever their size
                opener = (lambda info=info: io.BytesIO(archive.read(info))) if kind in RANDOM_ACCESS_KINDS else (lambda info=info: archive.open(info))
                try:
                    found, other, reason = profile_named(kind, opener, member, info.file_size, options, limits, budget_tables - len(tables))
                except PARSE_ERRORS as error:
                    found, other, reason = [], None, 'parse_failed: ' + type(error).__name__
                for table in found:
                    sheet = table.pop('sheet', None)
                    tables.append({'member': member + (' → ' + sheet if sheet else ''), 'bytes': info.file_size, **table})
                if other:
                    others[other] += 1
                if reason:
                    skipped.append({'member': member, 'reason': reason})
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
    kind = file_kind(rel)
    try:
        if kind == 'metadata':
            record['others'] = {ARCHIVE_METADATA: 1}
        elif kind in TABLE_KINDS:
            if budget_tables <= 0:
                record['skipped'].append({'reason': 'table_budget_for_dataset'})
                return record
            found, other, reason = profile_named(kind, lambda: path.open('rb'), path.name, path.stat().st_size, options, limits, budget_tables)
            record['tables'] = [{'member': table.pop('sheet', None), 'bytes': artifact.get('bytes'), **table} for table in found]
            if other:
                record['others'] = {other: 1}
            if reason:
                record['skipped'].append({'reason': reason})
        elif kind == 'sqlite':
            temporary = None
            try:
                target = path
                if path.name.lower().endswith('.gz'):
                    temporary = target = decompress_to_temp(path, limits['max_decompressed_bytes_per_file'])
                if target is None:
                    record['skipped'].append({'reason': 'decompression_budget'})
                else:
                    found = profile_sqlite(target, {**options, 'max_tables': budget_tables})
                    record['tables'] = [t for t in found if t['state'] == 'profiled']
                    record['skipped'] += [{'reason': t['reason']} for t in found if t['state'] == 'skipped']
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        elif kind == 'zip':
            tables, others, skipped = profile_zip(path, options, limits, budget_tables)
            record.update(tables=tables, others=dict(others), skipped=skipped)
        elif kind == 'adapter_missing':
            record['skipped'].append({'reason': 'format_adapter_missing'})
        else:
            record['others'] = {extension(path.name): 1}
    except PARSE_ERRORS as error:
        record['skipped'].append({'reason': 'parse_failed: ' + type(error).__name__})
    return record


DISK_STATE_REASONS = {'file_missing_on_disk', 'size_differs_from_receipt'}


PROFILE_CODE_END = '# ======== Rendering.'


def code_identity():
    """The identity of the profiling code: everything in this file above the rendering marker. A change there invalidates every
    reused profile; a change to how the page is written does not."""
    text = Path(__file__).read_text()
    return sha256(text[:text.index(PROFILE_CODE_END)].encode()).hexdigest()


def census_records(root):
    """The latest census record of this study and of the study workspaces beside it (read-only), one at a time; this study's first."""
    neighbours = sorted(p for p in root.parent.iterdir() if p.is_dir() and p.resolve() != root and (p / 'study.json').is_file()) if root.parent.is_dir() else []
    for folder in [root] + neighbours:
        try:
            pointer = read(folder / 'data/interim/dataset_structure/latest.json')
            record = read(folder / pointer['structure'])
        except (OSError, ValueError, KeyError):
            continue
        yield folder, record


def previous_profiles(root, options, limits, wanted=None):
    """Artifact profiles of earlier censuses, this study's and its neighbours', keyed by hash and file name.

    A profile is a function of the bytes, the file name, the profile options, the limits and this code, so one computed by
    another study for the same bytes is the same profile: a merged or reusing study need not profile copied files again.
    Profiles cut by a budget are left out. With `wanted` (keys of this study's files) only those are kept, so the records
    of large neighbouring studies are read one at a time and not held in memory.
    """
    profiles = {}
    current = {k: options.get(k) for k in PROFILE_OPTIONS}
    for folder, record in census_records(root):
        if record.get('code_sha256') != code_identity() or {k: (record.get('options') or {}).get(k) for k in PROFILE_OPTIONS} != current or record.get('limits') != limits:
            continue
        for d in record.get('datasets', []):
            if not d.get('profiled'):
                continue
            for a in d.get('artifacts', []):
                key = (a.get('sha256'), a['path'].rsplit('/', 1)[-1])
                if a.get('sha256') and (wanted is None or key in wanted) and not any(x.get('reason') in DISK_STATE_REASONS | BUDGET_REASONS for x in a.get('skipped', [])):
                    profiles.setdefault(key, (a, folder == root))
        del record
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
    wanted = {(a.get('sha256'), a['path'].rsplit('/', 1)[-1]) for d in catalogue['datasets'] for a in d.get('obtained_artifacts', []) if a.get('sha256')}
    reusable = previous_profiles(root, options, limits, wanted) if reuse else {}
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
                    if previous is not None and present and len(previous[0]['tables']) <= options['max_tables'] - tables_so_far:  # same bytes, name, options and code: the profile cannot differ
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


# ======== Rendering. Nothing below this line changes a file profile; profile reuse hashes only the code above it. ========


def column_row(column):
    kind = column['type'] if not column.get('declared_type') else f"{column['declared_type']} → {column['type']}"
    distinct = fmt_num(column['distinct']) if column['distinct'] is not None else (f"≥ {fmt_num(column['distinct_at_least'])}" if column.get('distinct_at_least') else '—')
    if column.get('key_like'):
        distinct += ' (unique)'
    parts = []
    numeric = column.get('numeric')
    if numeric:
        stats = [('min', numeric.get('min')), ('median', numeric.get('median')), ('mean', numeric.get('mean')), ('max', numeric.get('max'))]
        parts.append(' · '.join(f"{label} {('≈ ' if label == 'median' and numeric.get('median_is_estimate') else '') + fmt_num(round(value, 4) if label == 'mean' else value)}"
                                for label, value in stats if value is not None))
    if column.get('time_range'):
        parts.append(f"{column['time_range'][0]} … {column['time_range'][1]}")
    if column.get('labels'):
        parts.append('labels: ' + ', '.join(f"{cell(l['value'], 32)} ({fmt_num(l['count'])})" for l in column['labels'][:DEFAULTS['rendered_labels']])
                     + (f" … {fmt_num(len(column['labels']) - DEFAULTS['rendered_labels'])} more in the variable dictionary" if len(column['labels']) > DEFAULTS['rendered_labels'] else ''))
    elif column.get('text_length') and not column.get('key_like'):
        parts.append(f"text length {column['text_length'][0]}–{column['text_length'][1]}")
    values = fmt_num(column['values']) if column['values'] is not None else '—'
    missing = '—' if column['missing'] is None or column['values'] is None else (f"{column['missing'] / (column['values'] + column['missing']):.1%}" if column['values'] + column['missing'] else '—')
    name = f"`{cell(column['name'], 40)}`" + (f" {cell(column['label'], 60)}" if column.get('label') else '')
    return f"| {name} | {cell(kind, 48)} | {values} | {missing} | {distinct} | {' · '.join(p for p in parts if p) or '—'} |"


def table_heading(table):
    rows = ('≥ ' if table.get('rows_are_lower_bound') else '') + fmt_num(table['rows'])
    note = ''
    if table.get('sparse'):
        note = ' · sparse ARFF: declarations only'
    elif table.get('statistics') == 'footer':
        note = f" · {fmt_num(table.get('row_groups'))} row group(s); schema and the writer's footer statistics, values not read"
    elif table['profiled_rows'] < table['rows']:
        note = f" · statistics from the first {fmt_num(table['profiled_rows'])} rows"
    if table.get('ragged_rows'):
        note += f" · {fmt_num(table['ragged_rows'])} rows with a different width"
    if table.get('resource_types'):
        note += ' · resource types: ' + ', '.join(f'{k} ({v})' for k, v in table['resource_types'].items())
    if table.get('dataset_label'):
        note += f" · dataset label: {cell(table['dataset_label'], 60)}"
    return f"{rows} rows × {len(table['columns'])} columns · {table['format']}{note}"


REASON_TEXT = {
    'format_adapter_missing': 'no reader for this format',
    'table_budget_for_dataset': 'beyond the per-dataset table budget',
    'archive_member_budget': 'beyond the archive member budget',
    'member_over_parse_budget': 'archive member too large to read whole',
    'json_document_over_parse_budget': 'JSON document too large to parse',
    'workbook_over_parse_budget': 'workbook too large to read',
    'transport_file_over_parse_budget': 'SAS transport file too large to read',
    'parquet_footer_over_parse_budget': 'Parquet footer too large to read',
    'binary_content_not_a_text_table': 'binary content, not a text table',
    'html_is_not_tabular_data': 'an HTML page, not tabular data',
    'content_is_an_html_page_not_sas_transport': 'an HTML page saved under a SAS transport name (upstream content, not data)',
    'not_sas_transport': 'not a SAS transport file',
    'sas_transport_version_8_not_read': 'SAS transport version 8, not read',
    'malformed_sas_transport': 'malformed SAS transport file',
    'not_a_parquet_file': 'not a Parquet file',
    'not_an_excel_workbook': 'not an Excel workbook',
    'no_sheet_with_data': 'a workbook without data rows',
    'no_dataset_in_sas_transport': 'a SAS transport file without datasets',
    'empty_file': 'empty file',
    'json_items_are_not_objects': 'a JSON list of non-records',
    'no_records': 'no records',
    'no_arff_attributes': 'an ARFF file without attributes',
    'decompression_budget': 'over the decompression budget',
    'file_missing_on_disk': 'missing on disk',
    'size_differs_from_receipt': 'size differs from the receipt',
}


def reason_text(reason):
    if reason.startswith('parse_failed: '):
        return 'could not be parsed (' + reason.split(': ', 1)[1] + ')'
    return REASON_TEXT.get(reason, reason.replace('_', ' '))


def table_name(path, table):
    return path + ((' → ' + table['member']) if table.get('member') else '')


def merge_columns(column_lists, label_levels):
    """One column summary per position for tables with the same columns: counts add up, ranges widen, labels merge when complete."""
    merged = []
    for parts in zip(*column_lists):
        first = parts[0]
        kinds = Counter(p['type'] for p in parts)
        values, missing = [p['values'] for p in parts], [p['missing'] for p in parts]
        declared = {p.get('declared_type') for p in parts}
        out = {'name': first['name'], 'declared_type': declared.pop() if len(declared) == 1 else 'varies',
               'type': next(iter(kinds)) if len(kinds) == 1 else 'mixed (' + ', '.join(k for k, _ in kinds.most_common(2)) + ')',
               'values': sum(values) if None not in values else None, 'missing': sum(missing) if None not in missing else None,
               'distinct': None, 'distinct_at_least': max(((p['distinct'] if p['distinct'] is not None else p.get('distinct_at_least')) or 0) for p in parts) or None, 'key_like': False}
        if first.get('label'):
            out['label'] = first['label']
        numbers = [(p['numeric'], p['values'] or 0) for p in parts if p.get('numeric')]
        if numbers:
            lows = [n['min'] for n, _ in numbers if n.get('min') is not None]
            highs = [n['max'] for n, _ in numbers if n.get('max') is not None]
            weight = sum(w for n, w in numbers)
            mean = sum(n['mean'] * w for n, w in numbers) / weight if weight and all(n.get('mean') is not None for n, _ in numbers) else None
            out['numeric'] = {'min': min(lows) if lows else None, 'max': max(highs) if highs else None, 'mean': mean, 'sd': None, 'median': None, 'median_is_estimate': False}
        times = [p['time_range'] for p in parts if p.get('time_range')]
        if times:
            out['time_range'] = [min(t[0] for t in times), max(t[1] for t in times)]
        lengths = [p['text_length'] for p in parts if p.get('text_length')]
        if lengths:
            out['text_length'] = [min(l[0] for l in lengths), max(l[1] for l in lengths)]
        if all(p.get('labels') for p in parts):  # a table lists all its values when it has few: the union is exact
            counts = Counter()
            for p in parts:
                for label in p['labels']:
                    counts[label['value']] += label['count']
            if len(counts) <= label_levels:
                out['labels'] = [{'value': v, 'count': c} for v, c in counts.most_common()]
                out['distinct'], out['distinct_at_least'] = len(counts), None
        merged.append(out)
    return merged


def folder_of(name):
    """Where a table lives: its folder, or the archive and the folder inside it."""
    if ' → ' in name:
        outer, inner = name.split(' → ', 1)
        return outer + ' → ' + (posixpath.dirname(inner) or '.')
    return posixpath.dirname(name) or '.'


def families_of(d, options, minimum=3):
    """The dataset's tables as entries, named relative to the release folder: tables with the same columns and format
    (per-patient files, yearly extracts) form one family when there are at least `minimum`; the others stay single.
    Entries keep the order in which their first table appears."""
    base = dataset_base(d)
    groups = {}
    for a in d['artifacts']:
        rel = posixpath.relpath(a['path'], base) if base else a['path']
        for t in a['tables']:
            groups.setdefault((tuple(c['name'] for c in t['columns']), t['format']), []).append((table_name(rel, t), t))
    entries = []
    for members in groups.values():
        if len(members) >= minimum and members[0][1]['columns']:
            tables = [t for _, t in members]
            folders = Counter(folder_of(n) for n, _ in members)
            distinct = len({posixpath.basename(n.split(' → ')[-1]) for n, _ in members})
            entries.append({'family': True, 'names': [n for n, _ in members], 'folders': folders, 'distinct_names': distinct, 'format': tables[0]['format'],
                            'rows': sum(t['rows'] for t in tables), 'rows_are_lower_bound': any(t.get('rows_are_lower_bound') for t in tables),
                            'columns': merge_columns([t['columns'] for t in tables], options['label_levels'])})
        else:
            entries.extend({'family': False, 'names': [n], 'table': t} for n, t in members)
    return entries


def dataset_base(d):
    """The folder the dataset's held files share, so composition paths read from the release's own root."""
    folders = [posixpath.dirname(a['path']) for a in d['artifacts']]
    return posixpath.commonpath(folders) if folders and all(f for f in folders) else ''


def composition(d, limit=25):
    """Per top-level folder of the release: files, bytes, tables and the main file formats."""
    base = dataset_base(d)
    rows = {}
    for a in d['artifacts']:
        rel = posixpath.relpath(a['path'], base) if base else a['path']
        folder = rel.split('/', 1)[0] + '/' if '/' in rel else '(top level)'
        row = rows.setdefault(folder, {'files': 0, 'bytes': 0, 'tables': 0, 'formats': Counter()})
        row['files'] += 1
        row['bytes'] += a.get('bytes') or 0
        row['tables'] += len(a['tables'])
        row['formats'][extension(a['path'])] += 1
    out = ['| Folder | Files | Size | Tables | Main formats |', '|---|---|---|---|---|']
    ordered = sorted(rows.items(), key=lambda item: (-item[1]['bytes'], item[0]))
    for folder, row in ordered[:limit]:
        formats = ', '.join(f'{n} {ext}' for ext, n in row['formats'].most_common(4))
        out.append(f"| `{cell(folder, 60)}` | {fmt_num(row['files'])} | {fmt_bytes(row['bytes'])} | {fmt_num(row['tables'])} | {formats} |")
    if len(ordered) > limit:
        rest = [row for _, row in ordered[limit:]]
        out.append(f"| … {fmt_num(len(rest))} more folders | {fmt_num(sum(r['files'] for r in rest))} | {fmt_bytes(sum(r['bytes'] for r in rest))} | {fmt_num(sum(r['tables'] for r in rest))} | |")
    return base, out


def entry_heading(d, number, entry):
    if not entry['family']:
        return f"#### {d['id']} · table {number} · `{cell(entry['names'][0], 160)}`", table_heading(entry['table'])
    folders = ', '.join(('top level' if f == '.' else f"`{cell(f, 70)}`") + f" ({fmt_num(n)})" for f, n in entry['folders'].most_common(5)) + (' …' if len(entry['folders']) > 5 else '')
    rows = ('≥ ' if entry['rows_are_lower_bound'] else '') + fmt_num(entry['rows'])
    copies = (f" · only {fmt_num(entry['distinct_names'])} distinct file names: the same files appear in several folders or archives, so the row total counts them more than once"
              if entry['distinct_names'] < len(entry['names']) else '')
    return (f"#### {d['id']} · table {number} · {fmt_num(len(entry['names']))} tables with the same {fmt_num(len(entry['columns']))} columns",
            f"{rows} rows in total × {len(entry['columns'])} columns · {entry['format']} · in {folders} · e.g. `{cell(entry['names'][0], 100)}`{copies} · "
            'statistics combined over the tables (distinct counts are the largest per table unless every value is listed)')


def render_dataset(d, options):
    out = [f"### {d['id']} — {cell(d['name'], 120)}", '', f"<a id=\"{d['id'].lower()}\"></a>", '']
    where = ', '.join(f'`{loc}`' for loc in d['locators']) or 'see the dataset page'
    metadata = d['others'].get(ARCHIVE_METADATA, 0)
    others = ', '.join(f'{n} {ext}' for ext, n in d['others'].items() if ext != ARCHIVE_METADATA) or 'none'
    out.append(f"Held: {fmt_num(d['held_files'])} verified files ({fmt_bytes(d['held_bytes'])}) under {where}. Tables profiled: {fmt_num(d['tables'])} "
               f"({fmt_num(d['rows'])} rows, {fmt_num(d['columns'])} columns in total). Other files: {others}. "
               f"[Dataset page](DATASET_SELECTION.md#{d['id'].lower()}).")
    out.append('')
    if metadata:
        out += [f"{fmt_num(metadata)} archive metadata entries (macOS `__MACOSX/` resource forks, `.DS_Store`) are not data and were ignored.", '']
    if not d['profiled']:
        out += ['Not profiled in this run (restricted to other datasets); the counts above come from the receipt only.', '']
        return out
    if d['held_files'] > 1 or any(a['path'].lower().endswith('.zip') for a in d['artifacts']):
        base, table = composition(d)
        out += [f"**Composition** (folders under `{cell(base, 100)}/`):" if base else '**Composition:**', ''] + table + ['']
    if d['skipped']:
        groups = {}
        for item in d['skipped']:
            groups.setdefault(item['reason'], []).append(posixpath.basename(item.get('member') or item['path']))
        parts = []
        for reason, names in sorted(groups.items(), key=lambda g: -len(g[1])):
            examples = ', '.join(f'`{cell(n, 50)}`' for n in names[:3]) + (' …' if len(names) > 3 else '')
            parts.append(f"{fmt_num(len(names))} file(s): {reason_text(reason)} ({examples})")
        out += ['Not profiled: ' + '; '.join(parts) + '.', '']
    entries = families_of(d, options)
    if not entries:
        out += ['No tabular file was found among the held files: the release is code, documentation, archives without tables or a format without a reader (see the composition above).', '']
        return out
    tables = sum(len(e['names']) for e in entries)
    base = dataset_base(d)
    summary = (f"{fmt_num(tables)} tables" + (f" in {fmt_num(len(entries))} entries (tables with the same columns combined)" if len(entries) < tables else '')
               + ', column by column' + (f'; paths are relative to `{cell(base, 100)}/`' if base else ''))
    out += ['<details>', f"<summary>{summary}</summary>", '']
    for number, entry in enumerate(entries[:options['rendered_tables']], 1):
        heading, line = entry_heading(d, number, entry)
        columns = entry['columns'] if entry['family'] else entry['table']['columns']
        out += [heading, '', line, '', '| Column | Type | Values | Missing | Distinct | Summary |', '|---|---|---|---|---|---|']
        out += [column_row(c) for c in columns[:options['rendered_columns']]]
        if len(columns) > options['rendered_columns']:
            out.append(f"| … | | | | | {fmt_num(len(columns) - options['rendered_columns'])} more columns in the [variable dictionary](DATASET_VARIABLES.csv) |")
        out.append('')
    if len(entries) > options['rendered_tables']:
        out += [f"{fmt_num(len(entries) - options['rendered_tables'])} more entries, listed here without columns (every column is in the [variable dictionary](DATASET_VARIABLES.csv)):", '',
                '| Table | Rows | Columns | Format |', '|---|---|---|---|']
        for entry in entries[options['rendered_tables']:]:
            if entry['family']:
                name, rows, width, fmt = f"{fmt_num(len(entry['names']))} tables like `{cell(entry['names'][0], 100)}`", ('≥ ' if entry['rows_are_lower_bound'] else '') + fmt_num(entry['rows']), len(entry['columns']), entry['format']
            else:
                t = entry['table']
                name, rows, width, fmt = f"`{cell(entry['names'][0], 120)}`", ('≥ ' if t.get('rows_are_lower_bound') else '') + fmt_num(t['rows']), len(t['columns']), t['format']
            out.append(f"| {name} | {rows} | {fmt_num(width)} | {fmt} |")
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
           'the same source-bound preflight applies as to the files themselves (see [Licensing](DATA_LICENSES_AND_AI_USE.md)).', '',
           '**Variable dictionary:** [DATASET_VARIABLES.csv](DATASET_VARIABLES.csv) lists every column of every profiled table, one row per column (tables with the same columns '
           'as one entry), with its type, missingness, distinct values and ranges, for filtering in a spreadsheet. It is a derivative of the data under the same terms.', '']
    if data.get('reused_artifacts'):
        out += [f"{fmt_num(data['reused_artifacts'])} file profiles were reused from the previous census"
                + (f" ({fmt_num(data['reused_from_other_studies'])} of them from other studies' censuses of the same bytes)" if data.get('reused_from_other_studies') else '')
                + ': same bytes, options and profiling code, so the result cannot differ.', '']
    if data['receipt_files'] is not None and data['lock_files'] and data['lock_files'] != data['receipt_files']:
        out += [f"**The catalogue receipt is older than the holdings:** the acquisition lock lists {fmt_num(data['lock_files'])} files but the receipt knows {fmt_num(data['receipt_files'])}. "
                'Run `Scripts/validate_dataset_workspace.py` and `Scripts/document_dataset_selection.py`, then this report again, so every acquired file is profiled.', '']
    out += ['## At a glance', '', '| Dataset | Held files | Tables | Rows | Columns | Other files | Not profiled |', '|---|---|---|---|---|---|---|']
    for d in sorted(held, key=lambda x: x['id']):
        others = sum(n for ext, n in d['others'].items() if ext != ARCHIVE_METADATA)
        out.append(f"| [{d['id']}](#{d['id'].lower()}) | {fmt_num(d['held_files'])} ({fmt_bytes(d['held_bytes'])}) | {fmt_num(d['tables'])} | {fmt_num(d['rows'])} | {fmt_num(d['columns'])} | {fmt_num(others)} | {fmt_num(len(d['skipped']))} |")
    out += ['', f"Not held ({fmt_num(len(not_held))}): " + ', '.join(f"[{d['id']}](#{d['id'].lower()})" for d in sorted(not_held, key=lambda x: x['id'])) + '. See [Before you apply](#before-you-apply-what-is-known-about-the-datasets-that-are-not-held).' if not_held else '', '']
    out += ['## How to read the tables', '',
            '- **Type** is inferred from the values (integer, number, date, datetime, boolean, text; mixed when kinds disagree) or, for ARFF and SQLite, the declared type followed by the inferred one.',
            '- **Values** and **Missing** count non-missing cells and the share of missing cells (empty, NA, NULL, ?). **Distinct** counts different values; (unique) marks a column whose values never repeat, a candidate key.',
            '- **Summary** gives min, median, mean and max for numbers (≈ marks a median estimated from a sample), the first and last timestamp for dates, the labels and counts of low-cardinality columns, and the text length range otherwise.',
            '- **Composition** lists, per folder of the release, the files, their size, the tables found in them and the main file formats.',
            '- Tables with the same columns and format (per-patient files, yearly or per-site extracts) are combined into one entry when there are at least three: rows and missing values add up, '
            'ranges widen, labels merge when each table lists all its values, and distinct counts are otherwise the largest per table (≥).',
            '- Excel workbooks are read sheet by sheet with the first non-empty row as the header and Excel dates converted; Parquet files are described from their footer (schema, row count, '
            'the null counts and ranges the writer stored; values are not read); SAS transport files list each dataset with its variable labels and SAS dates converted.',
            '- Files inside ZIP archives are profiled like files on disk; delimited tables are streamed whatever their size, other formats are read whole up to the member budget. '
            'Code, documentation, images and formats without a reader are only counted; archive metadata (`__MACOSX/`, `.DS_Store`) is ignored.', '']
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


DICTIONARY_FIELDS = ['dataset_id', 'dataset_name', 'dataset_folder', 'table', 'tables_with_these_columns', 'distinct_file_names', 'format', 'rows', 'column', 'label', 'declared_type', 'type', 'values', 'missing',
                     'missing_share', 'distinct', 'unique', 'min', 'median', 'mean', 'max', 'first_time', 'last_time', 'labels', 'text_length']


def spreadsheet_safe(value):
    """Text that a spreadsheet would read as a formula is prefixed with an apostrophe; numbers stay numbers."""
    text = '' if value is None else str(value)
    if text[:1] in ('=', '+', '-', '@', '\t', '\r') and not FLOAT_RE.match(text):
        return "'" + text
    return text


def write_dictionary(data, target):
    """One row per column of every profiled table of the held datasets (tables with the same columns as one entry)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, DICTIONARY_FIELDS)
        writer.writeheader()
        for d in sorted((x for x in data['datasets'] if x['held_files'] and x['profiled']), key=lambda x: x['id']):
            base = dataset_base(d)
            for entry in families_of(d, data['options']):
                if entry['family']:
                    name, count, fmt, rows, columns = f"{entry['names'][0]} (and {len(entry['names']) - 1} more with the same columns)", len(entry['names']), entry['format'], entry['rows'], entry['columns']
                    distinct = entry['distinct_names']
                else:
                    t = entry['table']
                    name, count, fmt, rows, columns, distinct = entry['names'][0], 1, t['format'], t['rows'], t['columns'], 1
                for c in columns:
                    numeric, present = c.get('numeric') or {}, c['values']
                    writer.writerow({'dataset_id': d['id'], 'dataset_name': spreadsheet_safe(d['name']), 'dataset_folder': spreadsheet_safe(base), 'table': spreadsheet_safe(name),
                                     'tables_with_these_columns': count, 'distinct_file_names': distinct, 'format': fmt,
                                     'rows': rows, 'column': spreadsheet_safe(c['name']), 'label': spreadsheet_safe(c.get('label')), 'declared_type': spreadsheet_safe(c.get('declared_type')),
                                     'type': c['type'], 'values': '' if present is None else present, 'missing': '' if c['missing'] is None else c['missing'],
                                     'missing_share': f"{c['missing'] / (present + c['missing']):.4f}" if present is not None and c['missing'] is not None and present + c['missing'] else '',
                                     'distinct': c['distinct'] if c['distinct'] is not None else (f">={c['distinct_at_least']}" if c.get('distinct_at_least') else ''),
                                     'unique': 'yes' if c.get('key_like') else '', 'min': '' if numeric.get('min') is None else numeric['min'],
                                     'median': '' if numeric.get('median') is None else numeric['median'], 'mean': '' if numeric.get('mean') is None else round(numeric['mean'], 6),
                                     'max': '' if numeric.get('max') is None else numeric['max'], 'first_time': (c.get('time_range') or ['', ''])[0], 'last_time': (c.get('time_range') or ['', ''])[1],
                                     'labels': spreadsheet_safe('; '.join(f"{l['value']} ({l['count']})" for l in c.get('labels') or [])),
                                     'text_length': f"{c['text_length'][0]}-{c['text_length'][1]}" if c.get('text_length') else ''})
    return target


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
    variables = write_dictionary(data, root / 'docs/DATASET_VARIABLES.csv')
    shutil.copyfile(variables, folder / 'variables.csv')
    (root / 'data/interim/dataset_structure/latest.json').write_text(json.dumps(
        {'run': stamp, 'structure': str(structure.relative_to(root)), 'structure_sha256': digest(structure), 'document': 'docs/DATASET_STRUCTURE.md', 'document_sha256': digest(doc),
         'variables': 'docs/DATASET_VARIABLES.csv', 'variables_sha256': digest(variables),
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
        from datetime import date
        dated = root / 'reports' / f'dataset-variables-{date.today().isoformat()}.csv'
        dated.parent.mkdir(exist_ok=True)
        shutil.copyfile(root / 'docs/DATASET_VARIABLES.csv', dated)
        result['report'] = str(save_report(root, doc, title='Data structure: tables, columns, types and statistics', stem='dataset-structure', runs_dir='structure_reports',
                                           links={'DATASET_VARIABLES.csv': dated.name}))
        result['variables'] = str(dated)
    print(json.dumps(result))
    return result


if __name__ == '__main__':
    require_workspace(ROOT)
    main()
