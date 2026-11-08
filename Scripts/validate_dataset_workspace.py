#!/usr/bin/env python3
"""Build a dataset-oriented workspace and conservative, evidence-bound readiness.

Offline only: no inference, acquisition, repairs, code execution or data moves.
Provider bytes, prepared originals and sealed run inputs remain at their locators.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import xml.etree.ElementTree as ET
import zipfile

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root, snapshot_path, resolve_snapshot, snapshot_copy_path
ROOT = workspace_root(default=CODE_ROOT)
sys.path.insert(0, str(CODE_ROOT / 'Scripts'))
from datahunter.data_rights import LicenseRegistry, file_digest
from audit_dataset_downloads import LOCKS

COLLECTION = ROOT / 'data/raw/public_reference_collection'
PREPARED = COLLECTION / 'prepared'
WORKSPACE = ROOT / 'data/datasets'
POLICY = ROOT / 'configs/dataset_validation.json'
REGISTRY = ROOT / 'configs/dataset_study_usage.json'
REPORTS = ROOT / 'data/interim/dataset_validation'
MARKER = ('<!-- dataset-readiness:start -->', '<!-- dataset-readiness:end -->')


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def relative(path):
    return str(Path(path).relative_to(ROOT))


def safe_path(value):
    path = ROOT / value
    if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
        raise ValueError('unsafe_source_locator: ' + str(value))
    return path


def table_profile(stream, delimiter=',', sectioned=False):
    """Full row/width census, with no cell values retained in the report."""
    csv.field_size_limit(16 * 1024 * 1024)
    reader = csv.reader(stream, delimiter=delimiter, strict=True)
    header = None
    rows = ragged = replacement = comments = blanks = 0
    widths = Counter()
    for row in reader:
        if not row:
            blanks += 1
            continue
        if header is None and row[0].startswith('#'):
            comments += 1
            continue
        replacement += sum(cell.count('\ufffd') for cell in row)
        if header is None:
            header = row
            continue
        rows += 1
        widths[len(row)] += 1
        ragged += len(row) != len(header)
    if header is None:
        return {'state': 'failed', 'reason': 'no_table_header'}
    issues = []
    if sectioned:
        issues.append('sectioned_profile_not_rectangular_input')
    elif ragged:
        issues.append('ragged_rows_require_format_review')
    if len(set(header)) != len(header):
        issues.append('duplicate_header_names')
    if any(not col.strip() for col in header):
        issues.append('blank_header_names')
    if replacement:
        issues.append('provider_unicode_replacement_characters')
    if header[0].lstrip().startswith(('source =', 'source=', 'target =', 'target=')):
        issues.append('python_set_assignment_not_csv')
    return {'state': 'needs_review' if issues else 'passed_structure', 'rows': rows,
            'columns': len(header), 'header': header, 'ragged_rows': ragged,
            'row_widths': dict(widths), 'comment_lines': comments, 'blank_lines': blanks,
            'provider_replacement_characters': replacement, 'issues': issues}


def archive_profile(path, limits):
    """Bounded archive integrity, member safety and contained small-table checks."""
    cap = limits['max_decompressed_bytes_per_file']
    member_cap = limits['max_member_bytes_to_parse']
    suffix = path.name.lower()
    tables, members, total, skipped = [], 0, 0, 0
    unsafe = []
    is_zip = suffix.endswith(('.zip', '.xlsx', '.docx'))
    archive = zipfile.ZipFile(path) if is_zip else tarfile.open(path)
    with archive:
        entries = archive.infolist() if is_zip else archive.getmembers()
        if len(entries) > limits['max_archive_members']:
            return {'state': 'not_fully_checked', 'reason': 'archive_member_budget'}
        for entry in entries:
            name = entry.filename if is_zip else entry.name
            if Path(name).is_absolute() or '..' in Path(name).parts:
                unsafe.append(name)
            if not is_zip and (entry.issym() or entry.islnk()):
                unsafe.append(name)
            if (entry.is_dir() if is_zip else not entry.isfile()):
                continue
            members += 1
            size = entry.file_size if is_zip else entry.size
            if total + size > cap:
                skipped += 1
                continue
            total += size
            parse = name.lower().endswith(('.csv', '.tsv')) and size <= member_cap
            buffer = io.BytesIO() if parse else None
            source = archive.open(entry) if is_zip else archive.extractfile(entry)
            with source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    if buffer is not None:
                        buffer.write(block)
            if buffer is not None:
                try:
                    buffer.seek(0)
                    profile = table_profile(io.TextIOWrapper(buffer, encoding='utf-8-sig', newline=''),
                                            '\t' if name.lower().endswith('.tsv') else ',')
                except (UnicodeError, csv.Error) as error:
                    profile = {'state': 'needs_review', 'reason': type(error).__name__}
                tables.append({'member': name, **profile})
    issues = []
    if unsafe:
        issues.append('unsafe_or_link_members_never_extracted')
    if any(t['state'] != 'passed_structure' for t in tables):
        issues.append('contained_tables_require_format_review')
    result = {'state': 'not_fully_checked' if skipped else 'needs_review' if issues else 'passed_container',
              'members': members, 'checked_uncompressed_bytes': total,
              'members_not_read_budget': skipped, 'issues': issues,
              'unsafe_or_link_members': unsafe, 'contained_tables': tables,
              'scope': 'Archive streams read; ZIP CRC checked by reader. No extraction or program execution.'}
    if suffix.endswith('.xlsx'):
        result['scope'] += ' Workbook cell types/clinical meaning require a separate reader review.'
    return result


def structure(path, limits):
    name = path.name.lower()
    if name.endswith(('.zip', '.xlsx', '.docx', '.tar.gz', '.tgz')):
        return archive_profile(path, limits)
    if name.endswith(('.csv', '.tsv', '.csv.gz', '.sssom.tsv')):
        opener = gzip.open if name.endswith('.gz') else open
        with opener(path, 'rt', encoding='utf-8-sig', newline='') as stream:
            first = stream.readline(1024 * 1024)
            if first.lstrip().lower().startswith(('<!doctype html', '<html')):
                return {'state': 'failed', 'reason': 'html_is_not_tabular_data'}
            delimiter = '\t' if name.endswith('.tsv') else ';' if first.count(';') > first.count(',') else ','
            stream.seek(0)
            return {**table_profile(stream, delimiter,
                                   sectioned='recent_scottish' in str(path) and name == 'appendix a.csv'),
                    'delimiter': delimiter}
    if name.endswith(('.json', '.xml')):
        if path.stat().st_size > limits['max_decompressed_bytes_per_file']:
            return {'state': 'not_fully_checked', 'reason': 'structured_document_budget'}
        if name.endswith('.json'):
            json.loads(path.read_text(encoding='utf-8-sig'))
        else:
            ET.parse(path)
        return {'state': 'passed_document_parse', 'scope': 'Syntax only, no semantic/reference validation.'}
    if name.endswith('.ndjson.gz'):
        rows = size = 0
        with gzip.open(path, 'rb') as stream:
            for line in stream:
                size += len(line)
                if size > limits['max_decompressed_bytes_per_file']:
                    return {'state': 'not_fully_checked', 'reason': 'decompression_budget', 'lines_checked': rows}
                if line.strip():
                    json.loads(line)
                    rows += 1
        return {'state': 'passed_document_parse', 'lines': rows, 'scope': 'NDJSON syntax only, not FHIR conformance.'}
    if name.endswith('.arff'):
        attrs = rows = 0
        in_data = False
        with path.open(encoding='utf-8-sig') as stream:
            for line in stream:
                stripped = line.strip()
                if not stripped or stripped.startswith('%'):
                    continue
                if stripped.lower().startswith('@attribute'):
                    attrs += 1
                elif stripped.lower() == '@data':
                    in_data = True
                elif in_data:
                    rows += 1
        return {'state': 'format_specific_review', 'declared_attributes': attrs, 'data_lines': rows,
                'scope': 'ARFF declaration/data census, not a typed relational ingestion or gold check.'}
    if name.endswith('.pdf'):
        with path.open('rb') as stream:
            head = stream.read(8)
            stream.seek(max(0, path.stat().st_size - 2048))
            tail = stream.read()
        return {'state': 'passed_signature' if head.startswith(b'%PDF-') and b'%%EOF' in tail else 'failed',
                'scope': 'PDF signature/trailer only; not a dataset or fulltext comprehension test.'}
    if name.endswith(('.txt.gz', '.soft.gz')):
        size = 0
        with gzip.open(path, 'rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                size += len(block)
                if size > limits['max_decompressed_bytes_per_file']:
                    return {'state': 'not_fully_checked', 'reason': 'decompression_budget'}
        return {'state': 'passed_compression', 'uncompressed_bytes': size,
                'scope': 'Gzip checksum only; GEO/native table semantics require a format-specific adapter.'}
    return {'state': 'format_specific_review',
            'scope': 'Identity checked; native/software/text format not certified as workflow input.'}


def check_artifact(item, limits):
    result = dict(item)
    path = safe_path(item['path'])
    if not path.is_file():
        return {**result, 'integrity': 'missing', 'structure': {'state': 'not_checked'}}
    actual = file_digest(path)
    result.update(actual_sha256=actual, actual_bytes=path.stat().st_size)
    result['integrity'] = 'verified' if actual == item['sha256'] and path.stat().st_size == item['bytes'] else 'changed'
    if result['integrity'] != 'verified':
        result['structure'] = {'state': 'not_checked', 'reason': 'identity_mismatch'}
        return result
    if item.get('response_format') == 'empty_META_body_with_count_headers' and item['bytes'] == 0:
        headers = item.get('response_headers', {})
        if str(headers.get('total-count', '')).isdigit():
            result['structure'] = {'state': 'not_dataset_response',
                                   'reason': 'expected_empty_META_response_count_in_headers',
                                   'provider_total_count': int(headers['total-count']),
                                   'scope': 'Count receipt, not a failed JSON dataset or mutation records.'}
            return result
    try:
        result['structure'] = structure(path, limits)
    except (OSError, ValueError, csv.Error, UnicodeError, ET.ParseError, zipfile.BadZipFile,
            tarfile.TarError, RuntimeError, EOFError) as error:
        result['structure'] = {'state': 'failed', 'error_type': type(error).__name__,
                               'reason': 'deterministic_format_check_failed_no_repair'}
        if isinstance(error, csv.Error):
            result['structure']['csv_parser_diagnostic'] = str(error)
    return result


def group_for(dataset, policy):
    ident = dataset['id']
    for group, identifiers in policy['groups'].items():
        if ident in identifiers:
            return group
    for prefix, group in policy['dynamic_groups'].items():
        if ident.startswith(prefix):
            return group
    if dataset['completeness'] in {'not_obtained', 'metadata_only_or_not_obtained', 'metadata_only_patient_derivative_unacquired'}:
        return 'pending_access'
    return 'mapping_references' if ident in {'MEDITAB'} else 'needs_review'


def readiness(group, acquired, scopes, bad_integrity=False):
    """No successful parsing or development case makes an independent benchmark."""
    if bad_integrity:
        return 'blocked_integrity', 'blocked_integrity'
    if group == 'excluded':
        return 'not_appropriate', 'not_appropriate'
    if group == 'pending_access' or not acquired:
        return 'incomplete', 'incomplete'
    if group in {'metadata_only', 'standards_and_methods'}:
        return 'reference_only', 'not_a_primary_clinical_benchmark'
    if any(s['status'] == 'ready_scoped' for s in scopes):
        return 'ready_scoped', 'development_only_reference_or_holdout_unresolved'
    if group in {'synthetic_controls', 'tabular_controls'}:
        return 'needs_preparation_or_permissions', 'control_only_not_independent_clinical_gold'
    return 'needs_preparation_or_permissions', 'candidate_reference_review_and_frozen_split_required'


def scope_check(scope, artifacts, licenses, prepared_entries):
    files = scope.get('files')
    if files is None:
        path = safe_path(scope['path'])
        files = [relative(p) for p in sorted(path.rglob('*')) if p.is_file()] if path.is_dir() else []
    checks = [artifacts[p] for p in files if p in artifacts]
    all_identified = bool(files) and len(checks) == len(files)
    intact = all_identified and all(c['integrity'] == 'verified' for c in checks)
    parsed = intact and all(c['structure']['state'] == 'passed_structure' for c in checks)
    # Freeze the exact pre-existing source-only identities; new files block a scope.
    expected = ({p for p in prepared_entries if p.startswith(scope['path'].rstrip('/') + '/')}
                if 'path' in scope else {p for p in prepared_entries if p in scope['files']})
    declared = bool(files) and set(files) == expected
    roles_ok = declared and all(prepared_entries[p].get('prepared_role') in {'input', 'schema_input'}
                                or (scope.get('id') == 'pharmaverse_generated_response'
                                    and scope.get('dataset') == 'D03'
                                    and Path(p).name in {'rs_onco_irecist.csv', 'rs_onco_ca125.csv', 'supprs_onco_ca125.csv'})
                                for p in files)
    fingerprints = [c['actual_sha256'] for c in checks if c.get('integrity') == 'verified']
    permission = licenses.check(fingerprints, ['local_inference', 'local_embedding'])
    status = 'ready_scoped' if parsed and declared and roles_ok and permission['status'] == 'allowed' else 'blocked_or_incomplete'
    return {**scope, 'files': files, 'file_count': len(files), 'status': status,
            'integrity_complete': intact, 'rectangular_tables_complete': parsed,
            'prepared_manifest_bound': declared, 'permission_preflight': permission,
            'source_role_verified': roles_ok,
            'limits': 'Source development only; candidate vocabularies, targets, references and remote providers need their own runtime preflight.'}


def association(dataset, files, selectors):
    locators = list(dict.fromkeys(dataset.get('paths', []) + dataset.get('verified_artifacts', [])))
    matches = [f for f in files if any(f['path'] == p or f['path'].startswith(p.rstrip('/') + '/') for p in locators)]
    if selectors:
        matches = [f for f in matches if Path(f['path']).name in selectors]
    return locators, matches


def link(label, path, from_dir):
    return f'[{label}](<{os.path.relpath(path, from_dir)}>)'


def marked(path, content):
    text = path.read_text()
    block = MARKER[0] + '\n' + content + '\n' + MARKER[1]
    if MARKER[0] in text:
        before, rest = text.split(MARKER[0], 1)
        _, after = rest.split(MARKER[1], 1)
        text = before + block + after
    else:
        text += '\n\n' + block + '\n'
    path.write_text(text)


def retire_stale_navigation(datasets):
    """Remove only this command's obsolete two-file navigation folders."""
    expected = {d['id']: ROOT / d['workspace'] for d in datasets}
    retired = []
    for path in sorted(WORKSPACE.glob('*/*/manifest.json')):
        record = read(path)
        folder = path.parent
        if record.get('id') not in expected or expected[record['id']] == folder:
            continue
        if (record.get('workspace') != relative(folder) or not record.get('validation_id')
                or {p.name for p in folder.iterdir()} != {'README.md', 'manifest.json'}):
            raise ValueError('stale_navigation_has_unmanaged_files: ' + str(folder))
        retired.append({'path': relative(folder), 'manifest_sha256': file_digest(path),
                        'dataset': record['id'], 'reason': 'generated_category_changed_no_payload_present'})
        (folder / 'README.md').unlink()
        path.unlink()
        folder.rmdir()
        if not list(folder.parent.iterdir()):
            folder.parent.rmdir()
    return retired


def render(report, registry, policy):
    docs = ROOT / 'docs'
    counts = report['summary']
    for dataset in report['datasets']:
        folder=ROOT/dataset['workspace']
        write(folder/'manifest.json',dataset)
        # Storage folders carry pointers, not another copy of the dataset catalogue.
        (folder/'README.md').write_text('# '+dataset['id']+' storage record\n\n'+
            link('Current dataset catalogue',docs/'DATASET_SELECTION.md',folder)+
            ' · '+link('Priorities and access',docs/'DATASET_ACTION_PRIORITIES.md',folder)+
            '\n\nExact original locators, identities and scoped inputs: [manifest.json](manifest.json).\n')
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (WORKSPACE/'README.md').write_text('# Dataset storage records\n\n'+
        link('Dataset catalogue',docs/'DATASET_SELECTION.md',WORKSPACE)+
        ' · '+link('Priorities and access',docs/'DATASET_ACTION_PRIORITIES.md',WORKSPACE)+
        '\n\nCategory/ID folders contain machine-readable manifests and pointers. Originals remain at their hash-recorded acquisition locations.\n')



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    policy, registry = read(POLICY), read(REGISTRY)
    identifiers = [d['id'] for d in registry['datasets']]
    if len(set(identifiers)) != len(identifiers) or any(not re.fullmatch(r'[A-Za-z0-9_]+', i) for i in identifiers):
        raise ValueError('invalid_or_duplicate_dataset_id')
    declared = [i for ids in policy['groups'].values() for i in ids]
    if len(declared) != len(set(declared)) or set(declared) - set(identifiers):
        raise ValueError('invalid_category_assignment')
    if any(s['dataset'] not in identifiers for s in policy['input_scopes']):
        raise ValueError('unknown_scope_dataset')
    validation_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory = REPORTS / validation_id
    directory.mkdir(parents=True)
    manifest_path = PREPARED / 'metadata/manifest.json'
    if not manifest_path.exists():  # a fresh workspace has no prepared inputs yet
        write(manifest_path, {'schema_version': 1, 'files': []})
    manifest = read(manifest_path)
    locked = []
    evidence_paths = [POLICY, REGISTRY, manifest_path, Path(__file__), CODE_ROOT / 'AGENTS.md',
                      CODE_ROOT / 'Scripts/audit_dataset_downloads.py', CODE_ROOT / 'src/datahunter/data_rights.py',
                      CODE_ROOT / 'tests/test_dataset_workspace.py',
                      ROOT / 'configs/data_licenses.json', ROOT / 'configs/data_license_assets.json']
    for lock_name, base in LOCKS.items():
        lock = ROOT / lock_name
        if not lock.exists():
            raise ValueError('missing_download_lock: ' + lock_name)
        evidence_paths.append(lock)
        for entry in read(lock)['files']:
            locked.append({'path': relative(base / entry['path']), 'sha256': entry['sha256'],
                           'bytes': entry['bytes'], 'lock': lock_name,
                           **{k: entry[k] for k in ['response_format', 'response_headers'] if k in entry}})
    prepared = [{'path': relative(PREPARED / f['path']), 'sha256': f['sha256'],
                 'bytes': f['bytes'], 'prepared_role': f['role'], 'resource': f['resource']}
                for f in manifest['files']]
    by_path = {}
    for entry in locked + prepared:
        previous = by_path.setdefault(entry['path'], entry)
        if previous['sha256'] != entry['sha256'] or previous['bytes'] != entry['bytes']:
            raise ValueError('conflicting_expected_identity: ' + entry['path'])
    initial_evidence = {snapshot_path(p, ROOT)['path']: file_digest(p) for p in evidence_paths}
    print(f'Checking {len(locked)} locked and {len(prepared)} prepared identities; {len(by_path)} unique paths.', flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        checked = list(pool.map(lambda x: check_artifact(x, policy['limits']), by_path.values()))
    artifacts = {f['path']: f for f in checked}
    licenses = LicenseRegistry(mode='lenient')
    prepared_entries = {f['path']: f for f in prepared}
    scopes = [scope_check(s, artifacts, licenses, prepared_entries) for s in policy['input_scopes']]
    results = []
    for d in registry['datasets']:
        group = group_for(d, policy)
        locked_by_path = {f['path']: artifacts[f['path']] for f in locked}
        locators, matched = association(d, list(locked_by_path.values()), policy['source_file_selectors'].get(d['id']))
        input_scopes = [s for s in scopes if s['dataset'] == d['id']]
        bad = any(f['integrity'] != 'verified' for f in matched) or any(not s['integrity_complete'] for s in input_scopes)
        development, benchmark = readiness(group, bool(matched), input_scopes, bad)
        permission = licenses.check([f['actual_sha256'] for f in matched if f['integrity'] == 'verified'], ['local_inference', 'local_embedding'])
        families = [family for family, ids in policy['families'].items() if d['id'] in ids]
        if d['id'] == 'RICU_DEMOS':
            families = ['mimic', 'eicu']
        if not families:
            families = [d['id'].lower()]
        results.append({**{k: d[k] for k in ['id', 'name', 'scope', 'source_urls', 'uses', 'missing_components', 'next_action', 'access_route', 'registration_url', 'completeness', 'priority']},
                        'validation_id': validation_id, 'category': group,
                        'workspace': 'data/datasets/' + group + '/' + d['id'],
                        'optimization_status': development, 'benchmark_status': benchmark,
                        'independent_benchmark_certified': False,
                        'integrity_status': 'blocked' if bad else 'verified_associated_files' if matched else 'no_dataset_files_identified',
                        'source_locators': locators,
                        'missing_locators': [p for p in locators if not safe_path(p).exists()],
                        'association_scope': 'Registry path membership, not independent cohort completeness; archive member identities remain in structural findings.',
                        'obtained_artifacts': matched,
                        'format_counts': dict(Counter(f['structure']['state'] for f in matched)),
                        'input_scopes': input_scopes, 'associated_download_permissions': permission,
                        'review_note': policy['review_notes'].get(d['id'], ''),
                        'leakage_families': families,
                        'family_notes': [policy['shared_family_notes'][f] for f in families if f in policy['shared_family_notes']],
                        'benchmark_requirements': policy['independent_benchmark_requirements']})
    # Data files are read-only throughout. Check evidence/config stability before publishing.
    sources = [{**snapshot_path(p, ROOT), 'sha256': file_digest(p)} for p in evidence_paths]
    if any(initial_evidence[s['path']] != s['sha256'] for s in sources):
        raise ValueError('validation_configuration_changed_during_check')
    for source in sources:
        destination = directory / 'code_and_config' / snapshot_copy_path(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolve_snapshot(source, ROOT), destination)
    summary = {'dataset_entries': len(results), 'locked_artifacts': len(locked), 'prepared_artifacts': len(prepared),
               'locked_integrity': dict(Counter(artifacts[f['path']]['integrity'] for f in locked)),
               'prepared_integrity': dict(Counter(artifacts[f['path']]['integrity'] for f in prepared)),
               'structure': dict(Counter(f['structure']['state'] for f in checked)),
               'optimization': dict(Counter(d['optimization_status'] for d in results)),
               'benchmark': dict(Counter(d['benchmark_status'] for d in results)),
               'categories': dict(Counter(d['category'] for d in results))}
    report = {'schema_version': 1, 'validation_id': validation_id,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'model_calls': 0,
              'source_repairs': 0, 'data_moves': 0, 'payload_copies': 0,
              'scope': 'Fresh offline integrity/format checks plus curated task fit, source-only input permissions and independent benchmark blockers.',
              'summary': summary, 'evidence': sources,
              'independent_benchmark_certified': False, 'datasets': results, 'artifacts': checked}
    report['retired_generated_navigation'] = retire_stale_navigation(results)
    write(directory / 'report.json', report)
    write(directory / 'environment.json', {'python': sys.version, 'command': sys.argv,
                                           'model_calls': 0, 'prompt': None,
                                           'instructions': 'Offline organization and validation only; preserve all source bytes; report problems without repair.'})
    render(report, registry, policy)
    write(REPORTS / 'latest.json', {'validation_id': validation_id, 'report': relative(directory / 'report.json'),
                                  'report_sha256': file_digest(directory / 'report.json'), 'summary': summary,
                                  'source_repairs': 0, 'data_moves': 0, 'payload_copies': 0, 'model_calls': 0})
    print(json.dumps(summary, indent=2))
    return int(any(f['integrity'] != 'verified' for f in checked))


if __name__ == '__main__':
    raise SystemExit(main())
