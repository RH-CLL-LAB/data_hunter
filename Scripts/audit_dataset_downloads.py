"""Independently verify every retained download lock and reconcile all datasets.

No ETL, inference or record repair. Reports distinguish actual data, code/reference
resources, provider indexes and human access requirements. Raw data stay ignored.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import fcntl
import subprocess
import json
import os
import re
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_resources import contained, digest, write_json

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root, require_workspace
ROOT = workspace_root(default=CODE_ROOT)
COLLECTION = ROOT / 'data/raw/public_reference_collection'
BASE = ROOT / 'data/acquisitions'
OUTPUT = ROOT / 'data/interim/dataset_download_audit'


def location_parts(path):
    """Display path of a held file: relative to the collection folder when inside it, else to the workspace."""
    path = Path(path); collection = COLLECTION.relative_to(ROOT)
    return path.relative_to(collection).parts if path.is_relative_to(collection) else path.parts
LOCKS = {
    'configs/public_resources.lock.json': COLLECTION / 'downloaded_data',
    'configs/public_resources.priority1.lock.json': COLLECTION / 'acquisitions/priority1_2026-10-29',
    'configs/public_resources.remaining.lock.json': COLLECTION / 'acquisitions/remaining_2026-10-29',
    'configs/research_resources.lock.json': BASE,
    'configs/cbioportal_api.lock.json':COLLECTION/'acquisitions/cbioportal_api_2026-10-30',
    'configs/openml_parquet.lock.json':COLLECTION/'acquisitions/openml_parquet_2026-10-30',
    'configs/study_fulltexts.lock.json':COLLECTION/'acquisitions/study_usage_fulltexts_2026-11-04',
}

MANUAL = {
    'D38': ('https://physionet.org/content/mimiciv/3.1/', 'PhysioNet credentialing, CITI DSOR training and each applicable DUA; choose paper-specific MIMIC-III/IV versions.'),
    'D39': ('https://physionet.org/content/eicu-crd/2.0/', 'Full eICU needs PhysioNet credentials, CITI DSOR and DUA. The open demo is already held.'),
    'D41': ('https://amsterdammedicaldatascience.nl/', 'Institutional email, CITI DSOR, practicing intensivist reference and approved licence; then request files from the provider-specified DANS location. AMSTEL code is held.'),
    'D42': ('https://physionet.org/content/hirid/1.1.1/', 'PhysioNet credentials/training plus contributor review of the proposed study.'),
    'D43': ('https://physionet.org/content/sicdb/1.0.8/', 'PhysioNet contributor-review dataset: credentials/training, study approval and applicable agreement.'),
    'D44': ('https://physionet.org/content/picdb/1.1.0/', 'PhysioNet credentials, required training and project DUA.'),
    'D45': ('https://physionet.org/content/nwicu-northwestern-icu/0.1.0/', 'PhysioNet credentials, required training and project DUA.'),
    'D46': ('https://physionet.org/content/mimic-iii-ext-notes/1.0.0/', 'PhysioNet credentials, required training and project DUA.'),
    'D47': ('https://physionet.org/content/mimic-iv-fhir/2.1/', 'Full FHIR needs its project access agreement; the public demo is held. Match release to native source before pairing.'),
    'D25': ('https://ncri1.partners.org/ProACT/Document/DisplayLatest/', 'Register for PRO-ACT and accept its access/use terms; no approved session is established locally.'),
    'D26': ('https://biolincc.nhlbi.nih.gov/studies/sprint/', 'BioLINCC account and data request, research plan, applicable IRB/EC documentation and institutional agreement.'),
    'D27': ('https://biolincc.nhlbi.nih.gov/studies/accord/', 'BioLINCC account and data request, research plan, applicable IRB/EC documentation and institutional agreement.'),
    'D32': ('https://www.roche.com/innovation/clinical-trials/data-sharing/request', 'Request CLL14 (NCT02242942) participant data through Vivli; proposal/review and applicable agreements required.'),
    'D37': ('https://www.nature.com/articles/s41467-019-14225-8', 'Original Danish/CLL7 cohorts are not supplied as a public patient download. Seek source-owner/institutional authorization; preserve designated holdouts.'),
    'D40': ('https://www.ukbiobank.ac.uk/use-our-data/apply-for-access/', 'Register/apply as an eligible researcher; approved project, fees/agreement and permitted analysis environment.'),
}
for key, identifier in [('D20', 'ACTT'), ('D21', 'NCT04640168'), ('D22', 'NCT04593940')]:
    MANUAL[key] = ('https://accessclinicaldata.niaid.nih.gov/study-viewer/clinical_trials/' + identifier,
                   'Log in to AccessClinicalData@NIAID and submit a study-specific Data Access Request; required identity verification, research-use review and DUA before participant-file download. Public dictionaries/notices are separate.')
for key in ('D13', 'D14', 'D15', 'D16'):
    MANUAL[key] = ('https://c-path.org/tools-platforms/tb-pacts/', 'CP-DAP account, TB-PACTS access application, steering-committee approval and terms; confirm requested trial is included.')
for key, study in [('D17', '0027'), ('D18', '0030'), ('D19', '0051')]:
    MANUAL[key] = ('https://datashare.nida.nih.gov/study/nida-ctn-' + study,
                   'NIDA now instructs a DAC request: name/title/organization, study and dataset, intended use and willingness to accept its registration agreement. No request/email submitted.')
for key, trial in [('D28', 'NCT01578707'), ('D29', 'NCT01722487'), ('D30', 'NCT01611090'), ('D31', 'NCT02264574')]:
    MANUAL[key] = ('https://yoda.yale.edu/request/', 'Request ' + trial + ': proposal, personnel/COI, training, approval and institutional DUA; delivery environment set by provider.')
for key in ('D23', 'D24'):
    MANUAL[key] = ('https://data.projectdatasphere.org/projectdatasphere/html/registration', 'Project Data Sphere researcher registration, user agreement and access authorization; select the specified trial/challenge package.')

DATA_KINDS = {'dataset', 'dataset_component', 'dataset_bundle', 'dataset_supplement', 'repository_with_data'}


def git_archive_check(path, item, metadata):
    if not item.get('commit') or not item.get('mcp_evidence'):
        return None
    repo = item['url'].removeprefix('https://codeload.github.com/').split('/zip/')[0]
    source = next(x for x in metadata['repositories'] if x['repo'] == repo)
    expected = {x['path']: x for x in source['tree']['tree'] if x['type'] == 'blob'}
    checked = set()
    with zipfile.ZipFile(path) as archive:
        if archive.comment.decode('ascii') != item['commit']:
            raise ValueError('github_archive_commit_mismatch')
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.split('/', 1)[1]
            match = expected.get(name)
            if not match or info.file_size != match['size']:
                raise ValueError('github_archive_member_identity_mismatch')
            h = hashlib.sha1(b'blob ' + str(info.file_size).encode() + b'\0')
            with archive.open(info) as stream:
                for block in iter(lambda: stream.read(1024**2), b''):
                    h.update(block)
            if h.hexdigest() != match['sha']:
                raise ValueError('github_archive_git_blob_mismatch')
            checked.add(name)
    if checked != set(expected):
        raise ValueError('github_archive_file_set_mismatch')
    return {'commit': item['commit'], 'MCP_git_blobs_verified': len(checked)}


def verify(record):
    path = Path(record['absolute_path'])
    result = {k: v for k, v in record.items() if k not in {'absolute_path', 'inventory_absolute_path', 'quality_absolute_path'}}
    if not path.is_file() or path.is_symlink():
        return {**result, 'state': 'missing_or_symlink'}
    size, sha = path.stat().st_size, digest(path)
    result.update(actual_bytes=size, actual_sha256=sha,
                  state='verified' if size == record['bytes'] and sha == record['sha256'] else 'mismatch')
    if result['state'] == 'verified' and record.get('inventory_absolute_path'):
        inventory = Path(record['inventory_absolute_path'])
        if not inventory.is_file() or inventory.is_symlink() or digest(inventory) != record['inventory_sha256']:
            result.update(state='inventory_missing_or_changed')
    if result['state']=='verified' and record.get('quality_absolute_path'):
        quality=Path(record['quality_absolute_path'])
        if not quality.is_file() or digest(quality)!=record['quality_sha256']:result['state']='quality_evidence_missing_or_changed'
    if result['state'] == 'verified' and path.suffix.lower() in {'.csv', '.tsv', '.arff'}:
        with path.open('rb') as stream:
            header = stream.read(2048).lower().lstrip()
        if header.startswith((b'<!doctype html', b'<html')):
            result.update(content_state='unexpected_html_in_dataset', problem='HTTP response is a web page, not the expected tabular dataset; bytes retained as evidence')
    return result


def image_scope():
    config_path = ROOT / 'configs/tcia_image_downloads.json'
    if not config_path.exists():  # image acquisition is not part of every workspace
        return [], []
    config = json.loads(config_path.read_text())
    if not config.get('enabled', False):
        return [], []
    from acquire_tcia_images import expected_series, BASE as image_base, LOCK as image_lock
    jobs = expected_series(config)
    local = json.loads(image_lock.read_text()) if image_lock.exists() else {'files': []}
    records = [{**r, 'path': str((image_base / r['path']).relative_to(ROOT)),
                'absolute_path': str(image_base / r['path']),
                'inventory_absolute_path': str(image_base / r['inventory_path']),
                'lock': str(image_lock.relative_to(ROOT))} for r in local['files']]
    active=False
    active_path=image_base/'active-download.lock'
    if active_path.exists():
        with active_path.open('r') as stream:
            try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:active=True
            else:fcntl.flock(stream,fcntl.LOCK_UN)
    rows = []
    for collection in config['collections']:
        held = [r for r in records if r['collection'] == collection['id']]
        expected = sum(j['collection'] == collection['id'] for j in jobs)
        rows.append({'id': collection['id'], 'expected_series': expected,
                     'downloaded_series': len(held), 'zip_bytes': sum(r['bytes'] for r in held),
                     'provider_DICOM_MD5_checks': sum(r.get('provider_DICOM_MD5_checks', 0) for r in held),
                     'provider': collection['provider'],
                     'image_downloader_active':active,
                     'remaining_series_downloads':[{k:j[k] for k in ('id','url','path','expected_image_count')} for j in jobs if j['collection']==collection['id'] and j['id'] not in {r['id'] for r in held}],
                     'state': 'complete_current_manifest' if len(held) == expected else 'partial_download_running' if active else 'public_transfer_incomplete'})
    return records, rows


def cms_members(records, samples=range(1,21)):
    expected=set()
    for sample in samples:
        for year in (2008,2009,2010):
            expected.add(f'de1_0_{year}_beneficiary_summary_file_sample_{sample}.csv')
        for kind in ('inpatient_claims','outpatient_claims','prescription_drug_events','carrier_claims'):
            for suffix in ('a','b') if kind=='carrier_claims' else ('',):
                expected.add(f'de1_0_2008_to_2010_{kind}_sample_{sample}{suffix}.csv')
    held=set();mismatches=[]
    for record in records:
        if '/datasets/cms_de_synpuf/' not in record['path'] or record['state']!='verified':continue
        with zipfile.ZipFile(ROOT/record['path']) as archive:
            for name in archive.namelist():
                canonical=Path(name).name.lower().replace(' - copy','').replace('.copy.csv','.csv')
                if canonical in expected:
                    held.add(canonical)
                    basename=Path(record['path']).name.lower().removesuffix('.zip')
                    if canonical.removesuffix('.csv')!=basename.removesuffix('.csv'):
                        mismatches.append({'archive':record['path'],'member':name})
    return {'expected_data_members':len(expected),'held_data_members':len(held),
            'missing_data_members':sorted(expected-held),'archive_member_name_discrepancies':mismatches}


def reconcile(catalog, jobs, results, verified):
    rows = []
    for resource in catalog:
        key = resource['id']
        candidates = []
        locations = ([resource['local_evidence']] if resource.get('local_evidence') else []) + resource.get('additional_local_evidence', [])
        if locations:
            prefixes = [prefix / local for local in locations for prefix in (COLLECTION, COLLECTION / 'downloaded_data')]
            candidates += [r['path'] for r in verified if r['state'] == 'verified' and any(
                (ROOT / r['path']) == p or (ROOT / r['path']).is_relative_to(p) for p in prefixes)]
        original_artifacts = list(candidates)
        selected = [j for j in jobs if key in j.get('catalog_ids', [])
                    and j['acquisition'] != 'superseded_by_pinned_files']
        successes = [j for j in selected if results.get(j['id'], {}).get('state') == 'downloaded_verified'
                     and j['id'] in {r.get('id') for r in verified if r['state'] == 'verified' and not r.get('content_state')}]
        data = [j for j in successes if j['kind'] in DATA_KINDS]
        candidates += [str(BASE.relative_to(ROOT)) + '/' + j['path'] for j in successes]
        missing = [j['id'] for j in selected if j['kind'] in DATA_KINDS and j not in data]
        if key in MANUAL:
            link, action = MANUAL[key]
            state = 'partial_demo_or_code_full_data_requires_access' if original_artifacts or data else 'manual_access_required'
        elif missing:
            state, link, action = 'public_download_incomplete', resource['url'], 'Review the failed/missing artifact list; retry public transfer or use provider download route.'
        elif data:
            state, link, action = 'verified_public_data_or_fixture', resource['url'], 'No manual transfer required for the acquired scope; licence/AI and workflow validation remain separate.'
        elif original_artifacts or any(j['kind'] in {'specification_reference', 'supporting_code'} for j in successes):
            state, link, action = 'verified_existing_reference_or_data', resource['url'], 'Existing scope verified; see original preparation/reference limitations.'
        elif successes:
            state, link, action = 'provider_docs_only_data_missing', resource['url'], 'Provider documentation/index is held; no verified data package is established.'
        else:
            state, link, action = 'unresolved_download_scope', resource['url'], 'No verified package matched; resolve exact artifact/provider route before claiming acquisition.'
        if key == 'D09' and successes:
            state, action = 'verified_specification_not_patient_data', 'HL7 IG package held. No patient corpus is supplied by this specification; a separate published benchmark package is not established.'
        if key == 'D48' and candidates:
            state, action = 'verified_reference_code', 'ETL code/config/demo statistics held; MEDS patient output requires local execution, not a manual provider download.'
        rows.append({'id': key, 'name': resource['name'], 'state': state, 'source': link,
                     'manual_or_next_action': action, 'verified_artifacts': sorted(set(candidates)),
                     'missing_public_artifacts': missing})
    return rows


def local_links(paths):
    """Show each held package, without collapsing data and docs into one root."""
    groups = {}
    for path in sorted(set(paths)):
        relative = location_parts(path)
        offset = 2 if relative[0] == 'acquisitions' or relative[:2] == ('data', 'acquisitions') else 1  # batch folders and the study's acquisitions folder
        tail = relative[offset:]
        length = offset + (2 if tail and tail[0] in {'datasets', 'provider', 'references'} else 1)
        if tail and tail[0] == 'repositories':
            length = len(relative)
        groups.setdefault(relative[:length], []).append(path)
    locations = [os.path.commonpath(group) for group in groups.values()]
    return '<br>'.join(f'[{Path(*location_parts(path))}](../{path})' for path in locations) or '—'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--snapshot-in-progress', action='store_true', help='Report a clearly labelled live snapshot while the main acquisition is running')
    parser.add_argument('--wait-for-images', action='store_true', help='Retired option; raw images are outside current scope')
    args = parser.parse_args()
    if args.wait_for_images:
        parser.error('Image downloads/recovery retired; the audit never starts acquisition')
    started_at = datetime.now(timezone.utc).isoformat()
    manifest = json.loads((ROOT / 'configs/research_resources.json').read_text())
    from acquire_research_resources import validate_scope
    validate_scope(manifest)
    policy=json.loads((ROOT/'configs/resource_scope.json').read_text())
    receipt_path = BASE / 'acquisition-report.json'  # written by the acquisition script; absent until a first acquisition
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {'manifest_sha256': digest(ROOT / 'configs/research_resources.json'), 'results': []}
    receipt_stale = receipt['manifest_sha256'] != digest(ROOT / 'configs/research_resources.json')
    if receipt_stale and not args.snapshot_in_progress:
        raise ValueError('acquisition_report_is_stale_run_download_audit_first')
    records = []
    for name, base in LOCKS.items():
        if name in {'configs/openml_parquet.lock.json', 'configs/study_fulltexts.lock.json'} and not (ROOT/name).exists():continue
        for row in json.loads((ROOT / name).read_text())['files']:
            path = contained(base, row['path'])
            records.append({**row, 'path': str(path.relative_to(ROOT)), 'lock': name,
                            'absolute_path': str(path),
                            **({'quality_absolute_path':str(base/row['quality_path'])} if row.get('quality_sha256') else {})})
    image_records, image_rows = image_scope()
    records.extend(image_records)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        verified = list(pool.map(verify, records))
    for row in image_rows:
        row['independently_verified_series'] = sum(r.get('collection') == row['id'] and r['state'] == 'verified' for r in verified)
        if row['independently_verified_series'] != row['downloaded_series']:
            row['state'] = 'integrity_failure'
    failures = [r for r in verified if r['state'] != 'verified']
    content_findings = [r for r in verified if r.get('content_state')]
    by_id = {j['id']: j for j in manifest['resources']}
    git_checks = []
    for record in verified:
        item = by_id.get(record.get('id'))
        if item and item.get('mcp_evidence') == 'configs/mcp/all_dataset_resources.json' and item.get('commit'):
            metadata = json.loads((ROOT / item['mcp_evidence']).read_text())
            git_checks.append({'id': item['id'], **git_archive_check(ROOT / record['path'], item, metadata)})
    prepared_manifest = COLLECTION / 'prepared/metadata/manifest.json'  # written by the validator; absent in a new study
    prepared = json.loads(prepared_manifest.read_text()) if prepared_manifest.exists() else {'files': []}
    changed_prepared = [r['path'] for r in prepared['files'] if digest(COLLECTION / 'prepared' / r['path']) != r['sha256']]
    results = {r['id']: r for r in receipt['results']}
    if receipt_stale:
        journal_path=BASE/'operations.jsonl'
        if journal_path.exists():
            body=journal_path.read_bytes();body=body[:body.rfind(b'\n')+1]
            current_hash=digest(ROOT/'configs/research_resources.json')
            for line in body.splitlines():
                event=json.loads(line)
                if event.get('manifest_sha256')==current_hash and event['kind']=='acquisition_outcome':
                    results[event['details']['id']]=event['details']
        current_locked = {r.get('id'): r for r in verified if r.get('lock') == 'configs/research_resources.lock.json'}
        for job in manifest['resources']:
            held = current_locked.get(job['id'])
            if job['acquisition'] != 'direct':
                results[job['id']] = {'id':job['id'],'state':job['acquisition'],'reason':job['reason']}
            elif held and held['url'] == job['url'] and held['path'].endswith('/'+job['path']):
                results[job['id']] = {'id':job['id'],'state':'downloaded_verified'}
            elif job['id'] not in results or results[job['id']]['state'] == 'downloaded_verified':
                results[job['id']] = {'id':job['id'],'state':'not_yet_downloaded'}
    for job in manifest['resources']:  # jobs registered after the last acquisition run have no outcome yet
        if job['id'] not in results:
            results[job['id']] = ({'id': job['id'], 'state': job['acquisition'], 'reason': job.get('reason', '')} if job['acquisition'] != 'direct'
                                  else {'id': job['id'], 'state': 'not_yet_downloaded'})
    receipt['counts'] = dict(Counter(r['state'] for r in results.values()))
    catalog = json.loads((ROOT / 'configs/public_resource_acquisition.json').read_text())['resources']
    rows = reconcile(catalog, manifest['resources'], results, verified)
    cms = cms_members(verified, policy['cms_samples'])
    parquet_alternatives=[r for r in verified if r.get('lock')=='configs/openml_parquet.lock.json' and r['state']=='verified']
    cbio_api_path=COLLECTION/'acquisitions/cbioportal_api_2026-10-30/acquisition-report.json'
    cbio_api=json.loads(cbio_api_path.read_text()) if cbio_api_path.exists() else None
    cbio_verified=[r for r in verified if r.get('lock')=='configs/cbioportal_api.lock.json' and r['state']=='verified']
    for row in rows:
        if row['id']=='D33' and cbio_api and len(cbio_verified)==6:
            row['state']='original_export_missing_API_snapshot_verified'
            row['manual_or_next_action']='Original MAF unavailable from Git-LFS. Separate official API JSON snapshot held: 9,487 mutations, provider total count matched; 537 study samples. It is a different representation, not byte-identical MAF or mapping gold. Request original export only if required.'
        if row['id']=='D08':
            row['manual_or_next_action'] += f" CMS member coverage: {cms['held_data_members']}/{cms['expected_data_members']}."
            if cms['missing_data_members']:
                row['state']='public_download_incomplete'
                row['missing_public_artifacts'] += cms['missing_data_members']
                row['manual_or_next_action']='Acquire the distinct provider download-route archive; the stats/trends archive named sample20 contains sample1. Preserve both originals; no record repairs.'

    cleanup_path=ROOT/'configs/resource_cleanup.json'  # record of a past storage cleanup; optional
    cleanup=json.loads(cleanup_path.read_text()) if cleanup_path.exists() else {}
    verified_ids = {r.get('id') for r in verified if r['state'] == 'verified' and not r.get('content_state')}
    direct_jobs = [j for j in manifest['resources'] if j['acquisition'] == 'direct']
    automatic_remaining = [j['id'] for j in direct_jobs if j['id'] not in verified_ids or results[j['id']]['state'] != 'downloaded_verified']
    report = {'snapshot_started_at': started_at, 'checked_at': datetime.now(timezone.utc).isoformat(),
              'manifest_sha256': digest(ROOT / 'configs/research_resources.json'),
              'live_snapshot_before_acquisition_receipt':receipt_stale,
              'scope': 'Original clinical sources plus curated project-relevant follow-up resources; counts are not independent cohorts',
              'relevance_cleanup':cleanup,
              'locked_artifacts': len(verified), 'locked_bytes': sum(r['bytes'] for r in verified),
              'verification_counts': dict(Counter(r['state'] for r in verified)), 'failures': failures,
              'content_findings': content_findings,
              'OpenML_Parquet_alternatives':parquet_alternatives, 'cbioportal_API_alternative':cbio_api, 'cms_member_coverage':cms, 'tcia_collections': image_rows, 'github_archive_checks': git_checks, 'prepared_originals': len(prepared['files']),
              'changed_prepared_originals': changed_prepared, 'catalog': rows,
              'acquisition_counts': receipt['counts'], 'files': verified,
              'automatic_queue': {'direct_artifacts': len(direct_jobs), 'remaining_ids': automatic_remaining},
              'remaining_jobs': [{**j, 'outcome': results[j['id']]} for j in manifest['resources']
                                 if results[j['id']]['state'] != 'downloaded_verified'],
              'model_calls': 0}
    write_json(OUTPUT / 'report.json', report)
    print(json.dumps({k: report[k] for k in ['locked_artifacts', 'verification_counts', 'prepared_originals', 'changed_prepared_originals', 'acquisition_counts']}))
    return int(bool(failures or changed_prepared))


if __name__ == '__main__':
    require_workspace(ROOT)  # a clear message instead of a missing-file error when no study is active
    raise SystemExit(main())
