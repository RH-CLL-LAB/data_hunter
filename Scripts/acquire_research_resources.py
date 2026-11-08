"""Acquire the reviewed paper-followup manifest; never execute downloaded code.

Raw bodies stay in ignored data/. The tracked lock contains identities only.
Every attempt is journaled before transfer; failures do not stop other resources.
Acquisition/structural inspection never grants permission for workflow AI use.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import gzip
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import threading
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_resources import contained, digest, write_json

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root
ROOT = workspace_root(default=CODE_ROOT)
MANIFEST = ROOT / 'configs/research_resources.json'
LOCK = ROOT / 'configs/research_resources.lock.json'
BASE = ROOT / 'data/acquisitions'


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_scope(manifest):
    """Every active acquisition needs a recorded relevance review."""
    policy = json.loads((ROOT / 'configs/resource_scope.json').read_text())
    allowed = {r['id'] for r in policy['decisions'] if r['retain']}
    unknown = {r['id'] for r in manifest['resources']} - allowed
    if unknown:
        raise ValueError('resources_outside_reviewed_scope: ' + ', '.join(sorted(unknown)))


def require_download_review(item, locked):
    """Fail before transfer unless metadata supports this exact minimal artifact.

    Restoring a previously verified, unchanged identity is separate from adding
    a new dataset. Reference-only transfers are bounded to 5 MiB. None of these
    decisions replaces the source-bound AI-use preflight.
    """
    old = locked.get(item['id'])
    if old and old.get('url') == item['url'] and old.get('path') == item['path']:
        return {'basis': 'restore_exact_previously_verified_identity'}
    references = {'paper', 'repository_notice', 'repository_source', 'provider_document',
                  'provider_index', 'license_evidence', 'specification_reference', 'supporting_code'}
    if item.get('kind') in references and item.get('max_bytes', 64*1024**2) <= 5*1024**2:
        return {'basis': 'bounded_reviewed_reference_only'}
    registry_path = ROOT / 'configs/dataset_download_reviews.json'
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    approvals = [r for r in registry.get('approved_artifacts', []) if r.get('id') == item['id']]
    if len(approvals) != 1:
        raise ValueError('metadata_preflight_required_before_new_dataset_download: ' + item['id'])
    approval = approvals[0]
    budget=approval.get('max_bytes')
    if type(budget) is not int or budget<=0 or not approval.get('format'):
        raise ValueError('download_review_requires_explicit_positive_size_budget_and_format')
    for field in ('url', 'path', 'format', 'max_bytes'):
        if approval.get(field) != item.get(field):
            raise ValueError('download_review_artifact_identity_or_budget_changed: ' + field)
    checks = ('clinical_content', 'subject_and_grain', 'value_units_codes', 'time_semantics',
              'format_and_size', 'duplicate_overlap', 'test_gap', 'access')
    if (approval.get('decision') != 'approve_minimal_subset' or
        any(approval.get('checks', {}).get(k) is not True for k in checks) or
        not approval.get('use_case') or not approval.get('file_selection_reason') or
        not approval.get('metadata_evidence')):
        raise ValueError('download_review_incomplete_or_not_approved')
    for evidence in approval['metadata_evidence']:
        path = contained(ROOT, evidence['path'])
        if not path.is_file() or digest(path) != evidence['sha256']:
            raise ValueError('download_review_metadata_missing_or_changed')
    return {'basis': 'metadata_reviewed_minimal_subset', 'review_registry_sha256': digest(registry_path)}


def transfer_license_preflight(item, locked, reviews=None, registry=None):
    """Use the same exact-source gate for new dataset transfers on both paths.

    Public notices/documents remain inspectable without dataset AI clearance.
    A relevance/access checklist alone never clears a clinical payload.
    """
    if registry is None:
        import sys
        sys.path.insert(0, str(CODE_ROOT / 'src'))
        from datahunter.data_rights import LicenseRegistry
        registry = LicenseRegistry(ROOT / 'configs/data_licenses.json', ROOT / 'configs/data_license_assets.json', root=ROOT)
    old = locked.get(item['id'])
    fingerprint = old.get('sha256') if old and old.get('url') == item.get('url') and old.get('path') == item.get('path') else item.get('provider_sha256')
    operations = ['local_inference', 'local_embedding']
    if reviews is None:
        path = ROOT / 'configs/dataset_download_reviews.json'
        reviews = json.loads(path.read_text()) if path.exists() else {}
    source_ids = {r['id'] for r in reviews.get('reviews', []) if item['id'] in r.get('resource_aliases', [])}
    bindings = [r for r in reviews.get('approved_artifacts', []) if r.get('id') == item['id']]
    for binding in bindings:
        for resource in binding.get('download_license_resources', []):
            source_ids.update(registry.resources.get(resource, {}).get('catalog_ids', []))
    history_path = ROOT / 'configs/dataset_license_reviews.json'
    if source_ids and history_path.is_file():
        latest = {}
        for review in json.loads(history_path.read_text()).get('reviews', []):
            if review.get('applied') and not review.get('assets') and review.get('dataset_id') in source_ids and review.get('ai_evidence') == 'explicit_prohibition' and set(operations)&set(review.get('operations', [])):
                return {'mode':registry.mode,'status':'blocked','reason':'explicit_source_ai_prohibition_before_acquisition','model_calls':0}
            if review.get('applied') and not review.get('assets') and review.get('study_id', getattr(registry, 'default_study', 'medscrape')) == registry.study_id:
                latest[review['dataset_id']] = review
        if any(latest.get(id, {}).get('status') == 'prohibited' for id in source_ids):
            return {'mode':registry.mode,'status':'blocked','reason':'manual_source_prohibition_before_acquisition','model_calls':0}
    if fingerprint:
        if any(registry.resources[r].get('scope') == 'software_only' for r in registry.by_hash.get(fingerprint, [])):
            return {'mode':registry.mode,'status':'blocked','reason':'software_license_is_not_data_rights','model_calls':0}
        return registry.check([fingerprint], operations)
    approved = [r for r in reviews.get('approved_artifacts', []) if r.get('id') == item['id'] and
                all(r.get(k) == item.get(k) for k in ['url', 'path', 'format', 'max_bytes'])]
    resources = approved[0].get('download_license_resources', []) if len(approved) == 1 else []
    if not resources or any(r not in registry.resources for r in resources):
        return {'mode': registry.mode, 'status': 'blocked', 'reason': 'exact_artifact_license_binding_missing', 'model_calls': 0}
    if any(registry.resources[r].get('scope') == 'software_only' for r in resources):
        return {'mode':registry.mode,'status':'blocked','reason':'software_license_is_not_data_rights','model_calls':0}
    decisions = [registry._decision(r, op) for r in resources for op in operations]
    return {'mode': registry.mode, 'status': 'allowed' if all(d['state'] == 'allowed' for d in decisions) else 'blocked',
            'basis': 'exact_artifact_review_license_binding', 'decisions': decisions, 'policy_sha256': registry.policy_hash,
            'assets_sha256': registry.assets_hash, 'study_id': registry.study_id, 'model_calls': 0, 'post_download_identity_registration_required': True}


def validate(path, item):
    if path.stat().st_size > item.get('max_bytes', 64 * 1024**2):
        raise ValueError('download_size_budget_exceeded')
    if 'expected_bytes' in item and path.stat().st_size != item['expected_bytes']:
        raise ValueError('provider_size_mismatch')
    if item.get('provider_md5'):
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'md5').hexdigest()
        if actual != item['provider_md5']:
            raise ValueError('provider_md5_mismatch')
    if item.get('provider_sha256') and digest(path) != item['provider_sha256']:
        raise ValueError('provider_sha256_mismatch')
    if item.get('git_blob_sha1'):
        data = path.read_bytes()
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if actual != item['git_blob_sha1']:
            raise ValueError('MCP_git_blob_mismatch')
    if item['format'] == 'pdf':
        with path.open('rb') as stream:
            if not stream.read(1024).lstrip().startswith(b'%PDF-'):
                raise ValueError('not_a_pdf')
    if item['format'] == 'json':
        json.loads(path.read_text())
    if item['format'] == 'zip':
        return inspect_zip(path, item.get('csv_profile'), inspect_csv=item.get('inspect_csv', True),
                           max_uncompressed=item.get('max_uncompressed_bytes', 2 * 1024**3))
    if item['format'] in {'tar.gz', 'tar'}:
        members = []
        total = 0
        with tarfile.open(path, 'r|gz' if item['format'] == 'tar.gz' else 'r|') as archive:
            for info in archive:
                contained(path.parent / 'inspection_only', info.name)
                total += info.size
                if total > item.get('max_uncompressed_bytes', 16 * 1024**3) or len(members) >= 100000:
                    raise ValueError('archive_inspection_budget_exceeded')
                if info.isdir():
                    continue
                if not info.isfile():
                    raise ValueError('special_archive_member')
                with archive.extractfile(info) as stream:
                    sha = hashlib.file_digest(stream, 'sha256').hexdigest()
                members.append({'path': info.name, 'bytes': info.size, 'sha256': sha})
        return {'members': members, 'uncompressed_bytes': total, 'extracted': False}
    if item['format'] == 'gzip':
        with gzip.open(path, 'rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        return {'uncompressed_sha256': sha, 'gzip_integrity': 'passed', 'extracted': False}
    if item['format'] == 'arff':
        with path.open('rb') as stream:
            header = stream.read(65536).lower()
        if b'@relation' not in header or b'<html' in header:
            raise ValueError('not_an_arff_dataset')
    return None


def inspect_zip(path, profile=None, *, inspect_csv=True, max_uncompressed=2 * 1024**3):
    """Read member identities and CSV shapes only; no extraction or row export."""
    members, tables = [], []
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 100000 or sum(x.file_size for x in entries) > max_uncompressed:
            raise ValueError('archive_inspection_budget_exceeded')
        if len({x.filename for x in entries}) != len(entries):
            raise ValueError('duplicate_archive_member')
        for info in entries:
            contained(path.parent / 'inspection_only', info.filename)
            if (info.external_attr >> 16) & 0o170000 not in (0, 0o100000, 0o040000):
                raise ValueError('special_archive_member')
            if info.is_dir():
                continue
            # Reading to EOF checks the ZIP CRC, without saving derived copies.
            with archive.open(info) as stream:
                sha = hashlib.file_digest(stream, 'sha256').hexdigest()
            members.append({'path': info.filename, 'bytes': info.file_size, 'sha256': sha})
            if inspect_csv and info.filename.lower().endswith('.csv') and not info.filename.startswith('__MACOSX/'):
                try:
                    with archive.open(info) as stream:
                        reader = csv.reader(io.TextIOWrapper(stream, encoding='utf-8-sig'))
                        header = next(reader)
                        rows = ragged = 0
                        selected = profile if profile and profile['member'] == info.filename else None
                        groups = {k: set() for k in selected.get('distinct_groups', {})} if selected else {}
                        missing, analytes = Counter(), Counter()
                        repeated = 0
                        for row in reader:
                            rows += 1
                            ragged += len(row) != len(header)
                            if selected and len(row) == len(header):
                                record = dict(zip(header, row))
                                for group, fields in selected.get('distinct_groups', {}).items():
                                    value = tuple(record[field] for field in fields)
                                    if group == selected.get('candidate_key'):
                                        repeated += value in groups[group]
                                    groups[group].add(value)
                                for field in selected.get('required_fields', []):
                                    if record[field].strip().lower() in selected.get(
                                            'missing_tokens', ('', 'null', 'na', 'nan')):
                                        missing[field] += 1
                                field = selected.get('analyte_field')
                                if field and re.search(selected['analyte_pattern'], record[field], re.I):
                                    analytes[record[field]] += 1
                        tables.append({'path': info.filename, 'rows': rows,
                                       'columns': header, 'ragged_rows': ragged})
                        if selected:
                            tables[-1]['deterministic_profile'] = {
                                'specification': selected,
                                'distinct_counts': {k: len(v) for k, v in groups.items()},
                                'repeated_candidate_keys': repeated,
                                'configured_missing_token_counts': dict(missing),
                                'candidate_analyte_name_counts': dict(analytes),
                                'clinical_mapping_validated': False}
                except (UnicodeError, csv.Error, StopIteration) as error:
                    tables.append({'path': info.filename, 'inspection_problem': type(error).__name__})
    return {'members': members, 'tables': tables, 'extracted': False,
            'uncompressed_bytes': sum(x['bytes'] for x in members)}


def acquire(item, locked, base=BASE, *, execute=False, journal=None, license_context=None):
    if item.get('acquisition') != 'direct':
        return {'id': item['id'], 'state': item['acquisition'], 'reason': item['reason']}
    path = contained(base, item['path'])
    old = locked.get(item['id'])
    if old and (old['path'] != item['path'] or old['url'] != item['url']):
        raise ValueError('locked_source_identity_changed_create_a_new_resource_version')
    if path.exists():
        if not old:
            raise ValueError('untracked_existing_file_requires_review')
        if digest(path) != old['sha256'] or path.stat().st_size != old['bytes']:
            raise ValueError('locked_bytes_changed_refusing_overwrite')
        inspection = validate(path, item)
        return {**old, 'state': 'downloaded_verified', 'inspection': inspection}
    if not execute:
        return {'id': item['id'], 'state': 'planned'}
    review = require_download_review(item, locked)
    reference_kinds = {'paper', 'repository_notice', 'repository_source', 'provider_document',
                       'provider_index', 'license_evidence', 'specification_reference', 'supporting_code'}
    if item.get('kind') not in reference_kinds or item.get('max_bytes', 64*1024**2) > 5*1024**2:
        registry = None
        if license_context:
            import sys
            sys.path.insert(0, str(CODE_ROOT/'src'))
            from datahunter.data_rights import LicenseRegistry
            registry = LicenseRegistry(ROOT/'configs/data_licenses.json',ROOT/'configs/data_license_assets.json',root=ROOT,**license_context)
        rights = transfer_license_preflight(item, locked, registry=registry)
        if rights['status'] != 'allowed':
            raise ValueError('dataset_license_required_before_transfer: ' + item['id'])
        review = {**review, 'license_preflight': rights}
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + '.part')
    if partial.exists():
        raise ValueError('partial_transfer_retained_requires_review')
    if not item['url'].startswith('https://'):
        raise ValueError('https_required')
    if journal:
        journal('transfer_started', {'id': item['id'], 'url': item['url'], 'path': item['path'],
                                     'download_preflight': review})
    result = subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                             '--proto', '=https', '--proto-redir', '=https',
                             '--connect-timeout', '20', '--max-time', '600',
                             '--max-filesize', str(item.get('max_bytes', 64 * 1024**2)),
                             '--output', str(partial), item['url']], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'curl_exit_{result.returncode}; partial retained; {result.stderr[-160:]}')
    inspection = validate(partial, item)
    sha = digest(partial)
    if old and (sha != old['sha256'] or partial.stat().st_size != old['bytes']):
        raise ValueError('restored_bytes_differ_from_lock_partial_retained')
    authority = ('provider_SHA256' if item.get('provider_sha256') else
                 'GitHub_MCP_git_blob' if item.get('git_blob_sha1') else
                 'provider_MD5_plus_local_SHA256' if item.get('provider_md5') else
                 'local_first_acquisition_SHA256_not_provider_checksum')
    record = {'id': item['id'], 'path': item['path'], 'url': item['url'],
              'bytes': partial.stat().st_size, 'sha256': sha, 'hash_authority': authority,
              'acquired_at': old['acquired_at'] if old else now()}
    partial.replace(path)
    return {**record, 'state': 'downloaded_verified', 'inspection': inspection}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Download missing approved direct URLs')
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 9))
    parser.add_argument('--id', action='append', help='Limit network transfers to these active resource IDs; still inventory all retained resources')
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    validate_scope(manifest)
    if args.id and set(args.id) - {j['id'] for j in manifest['resources']}:
        parser.error('Requested ID is outside the active relevant manifest')
    identities = [x['id'] for x in manifest['resources']]
    paths = [x['path'] for x in manifest['resources'] if x['acquisition'] == 'direct']
    if len(set(identities)) != len(identities) or len(set(paths)) != len(paths):
        raise ValueError('duplicate_manifest_identity_or_path')
    lock = json.loads(LOCK.read_text()) if LOCK.exists() else {'schema_version': 1, 'files': []}
    locked = {x['id']: x for x in lock['files']}
    results, events = {}, []
    BASE.mkdir(parents=True, exist_ok=True)
    # Content-addressed local snapshots preserve exact acquisition instructions/code.
    for source in (MANIFEST, Path(__file__), CODE_ROOT / 'Scripts/public_resources.py',
                   ROOT / 'configs/resource_scope.json',
                   ROOT / 'configs/dataset_download_reviews.json',
                   ROOT / 'configs/dataset_metadata_evidence.json'):
        target = BASE / 'provenance' / (digest(source) + source.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError('provenance_snapshot_collision')
        if not target.exists():
            target.write_bytes(source.read_bytes())
    journal_lock = threading.Lock()
    def journal(kind, details):
        event = {'at': now(), 'kind': kind, 'details': details,
                 'manifest_sha256': digest(MANIFEST), 'script_sha256': digest(Path(__file__)),
                 'download_review_sha256': digest(ROOT / 'configs/dataset_download_reviews.json'),
                 'common_code_sha256': digest(CODE_ROOT / 'Scripts/public_resources.py')}
        with journal_lock:
            with (BASE / 'operations.jsonl').open('a') as stream:
                stream.write(json.dumps(event, sort_keys=True) + '\n')
            events.append(event)
    def work(item):
        try:
            return acquire(item, locked, execute=args.execute and (not args.id or item['id'] in args.id), journal=journal)
        except Exception as error:
            return {'id': item['id'], 'state': 'failed', 'problem': str(error)}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, item): item for item in manifest['resources']}
        for future in as_completed(futures):
            item, result = futures[future], future.result()
            if result['state'] == 'downloaded_verified':
                locked[item['id']] = {k: v for k, v in result.items() if k not in {'state', 'inspection'}}
                # Persist each successful identity immediately, before moving on.
                write_json(LOCK, {'schema_version': 1, 'scope': manifest['scope'],
                                  'files': list(locked.values())})
            results[item['id']] = result
            journal('acquisition_outcome', result)
            print(item['id'], result['state'], flush=True)
    report = {'checked_at': now(), 'manifest_sha256': digest(MANIFEST),
              'results': list(results.values()), 'counts': dict(Counter(x['state'] for x in results.values())),
              'scope': 'Acquisition and deterministic inventory only; no inference, ETL, repairs or benchmark execution'}
    write_json(BASE / 'acquisition-report.json', report)
    print(json.dumps(report['counts']))
    return int(any(x['state'] == 'failed' for x in results.values()))


if __name__ == '__main__':
    raise SystemExit(main())
