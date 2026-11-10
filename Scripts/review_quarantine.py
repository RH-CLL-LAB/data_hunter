#!/usr/bin/env python3
"""List what the download agent left in quarantine and, on a human-ticked request, release or discard it.

`list` (the default) scans data/quarantine/download_agent/<run>/ against each run's report.json and the
acquisition lock, and writes data/interim/quarantine_review/latest.json: every staged file with its run,
job id, dataset IDs, bytes, SHA-256 and one state:

  pending_review        a delivery that is not held yet; it can be released after the checks below
  superseded_duplicate  byte-identical to a file already held and locked; it can be discarded
  identity_changed      the same job is locked with different bytes; never released automatically
  staged_bytes_changed  the staged file no longer matches the run record; never released
  missing_file          the run recorded a delivery whose staged file is gone
  orphan                a file in the quarantine folder that no run record describes

The site's Quarantine page shows this listing with tick boxes and saves a `datahunter_quarantine_release`
request. `--request FILE` validates it against the current listing (same listing hash, allowed action per
state, registered study); `--apply` then releases the ticked pending deliveries into data/acquisitions
(hash re-verification, the acquisition format checks, an optional second download compared byte for byte,
lock and acquisition-report update, licence-asset binding from the exact-artifact approval) and deletes the
ticked superseded duplicates. Nothing else is touched. A release grants no AI use: the source-bound
preflight still runs before inference or embeddings.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_resources import contained, digest, write_json  # noqa: E402
import acquire_research_resources as acquisition  # noqa: E402
from run_download_agent import RISKS  # noqa: E402  (the technical-exception explanations shown at download time)

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root, require_workspace  # noqa: E402
from datahunter.research_studies import default_study, append_log, normalize as normalize_study, registered as registered_study, fingerprint as study_fingerprint  # noqa: E402

ROOT = workspace_root(default=CODE_ROOT)
QUARANTINE = 'data/quarantine/download_agent'
LISTING_DIR = 'data/interim/quarantine_review'
KIND = 'datahunter_quarantine_release'
ACTIONS = {'release': 'pending_review', 'discard_superseded': 'superseded_duplicate'}
STATE_MEANING = {
    'pending_review': 'It is not held yet: nothing uses it until a reviewed release moves it into the locked holdings.',
    'superseded_duplicate': 'A byte-identical copy is already held and locked, so this staged copy is redundant and can be discarded.',
    'identity_changed': 'The same job is now locked with different bytes; review the run record manually before anything is released.',
    'staged_bytes_changed': 'The staged bytes no longer match the run record; the file is not what was delivered and is never released.',
    'missing_file': 'The run recorded this delivery but the staged file is gone.',
    'orphan': 'No download-agent run record describes this file; it is never released from the page.'}


def why_quarantined(status_at_download, risks, state, held_as=()):
    """Plain-language reason: how the file got here (download-time status and accepted exceptions) and what its state means now."""
    if status_at_download == 'downloaded_quarantined':
        how = ('Transferred under an accepted technical exception: ' + ' '.join(RISKS.get(r, r) for r in risks)) if risks else 'Transferred under an accepted technical exception.'
    elif status_at_download == 'downloaded_rights_unresolved':
        how = 'Acquired under the explicit unresolved-rights option: open access, identity verified, but no licence decision is recorded, so AI use stays blocked until a licence review registers the terms.'
    elif status_at_download == 'downloaded_staged_verified':
        how = 'Verified against the provider identity at download, but the download agent stages every new file here until it is registered into the locked holdings.'
    elif status_at_download:
        how = f'Download-agent status at delivery: {status_at_download}.'
    else:
        how = 'Not delivered by a recorded download-agent run.'
    meaning = STATE_MEANING.get(state, '')
    if state == 'superseded_duplicate' and held_as:
        meaning = meaning[:-1] + f" (held as {', '.join(held_as)})."
    return (how + ' ' + meaning).strip()
LOCAL_AUTHORITY = 'local_first_acquisition_SHA256_not_provider_checksum'


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def inventory(root, write=True):
    """Every staged quarantine file with its run record, dataset IDs and one state; optionally written as the current listing."""
    root = Path(root).resolve()
    lock_path = root / 'configs/research_resources.lock.json'
    locked = {f['id']: f for f in read(lock_path)['files']} if lock_path.exists() else {}
    by_sha = {}
    for f in locked.values():
        by_sha.setdefault(f['sha256'], []).append(f['id'])
    manifest_path = root / 'configs/research_resources.json'
    jobs = {r['id']: r for r in read(manifest_path)['resources']} if manifest_path.exists() else {}
    reviews_path = root / 'configs/dataset_download_reviews.json'
    reviews = read(reviews_path) if reviews_path.exists() else {}
    datasets = {}
    for review in reviews.get('reviews', []):
        for alias in review.get('resource_aliases', []):
            datasets.setdefault(alias, []).append(review['id'])
    bindings = {a['id']: a.get('download_license_resources', []) for a in reviews.get('approved_artifacts', [])}
    items = []
    base = root / QUARANTINE
    for run_dir in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
        run = run_dir.name
        report_path = root / 'runs/downloads' / run / 'report.json'
        records = {}
        if report_path.is_file():
            for artifact in read(report_path).get('unique_artifacts', []):
                record = artifact.get('record')
                if record and str(artifact.get('status', '')).startswith('downloaded'):
                    records[record['path']] = {**artifact, 'record': record}
        staged = sorted(p for p in run_dir.rglob('*') if p.is_file() and p.name != '.DS_Store')
        seen = set()
        for path in staged:
            relative = path.relative_to(run_dir).as_posix()
            artifact = records.get(relative)
            sha = digest(path)
            item = {'run': run, 'path': str(path.relative_to(root)), 'job_path': relative, 'bytes': path.stat().st_size, 'sha256': sha}
            if artifact is None:
                item.update(id=None, state='orphan', dataset_ids=[], technical_risks=[], status_at_download=None, reason=why_quarantined(None, [], 'orphan'))
            else:
                record = artifact['record']
                seen.add(relative)
                item.update(id=record['id'], url=record.get('url'), hash_authority=record.get('hash_authority'), acquired_at=record.get('acquired_at'),
                            dataset_ids=sorted(datasets.get(record['id'], [])), technical_risks=artifact.get('technical_risks', []), status_at_download=artifact.get('status'),
                            license_resources=bindings.get(record['id'], []))
                old = locked.get(record['id'])
                if sha != record.get('sha256') or item['bytes'] != record.get('bytes'):
                    item['state'] = 'staged_bytes_changed'
                elif old is not None:
                    item['state'] = 'superseded_duplicate' if old['sha256'] == sha else 'identity_changed'
                elif sha in by_sha:
                    item['state'] = 'superseded_duplicate'
                    item['held_as'] = by_sha[sha]
                else:
                    item['state'] = 'pending_review'
                if record['id'] in jobs and (jobs[record['id']].get('path') != record['path'] or jobs[record['id']].get('url') != record.get('url')):
                    item['state'] = 'identity_changed'
                item['reason'] = why_quarantined(item['status_at_download'], item['technical_risks'], item['state'], item.get('held_as') or ([record['id']] if item['state'] == 'superseded_duplicate' and old is not None else ()))
            items.append(item)
        for relative, artifact in records.items():
            if relative not in seen:
                record = artifact['record']
                items.append({'run': run, 'id': record['id'], 'path': str((run_dir / relative).relative_to(root)), 'job_path': relative, 'bytes': record.get('bytes'),
                              'sha256': record.get('sha256'), 'state': 'missing_file', 'dataset_ids': sorted(datasets.get(record['id'], [])),
                              'technical_risks': artifact.get('technical_risks', []), 'status_at_download': artifact.get('status'),
                              'reason': why_quarantined(artifact.get('status'), artifact.get('technical_risks', []), 'missing_file')})
    listing = {'schema_version': 1, 'kind': 'datahunter_quarantine_listing', 'checked_at': now(), 'workspace_quarantine': QUARANTINE,
               'summary': dict(Counter(i['state'] for i in items)), 'bytes': sum(i.get('bytes') or 0 for i in items), 'items': items,
               'note': 'States describe staged copies only. A release moves a delivery into the locked holdings after the checks; it grants no AI use.'}
    if write:
        folder = root / LISTING_DIR / stamp()
        write_json(folder / 'listing.json', listing)
        write_json(root / LISTING_DIR / 'latest.json', {'listing': str((folder / 'listing.json').relative_to(root)), 'listing_sha256': digest(folder / 'listing.json'),
                                                          'checked_at': listing['checked_at'], 'summary': listing['summary'], 'bytes': listing['bytes']})
    return listing


def refresh_dataset_entries(root, dataset_ids):
    """Bring the dataset registry (configs/dataset_study_usage.json) in line with the lock after acquisitions or releases.

    For each dataset with an exact-artifact review: held = locked alias jobs; paths = their acquisition folders; completeness,
    missing parts and next action from the review's full_release record (unbound, unacknowledged, unsupported, not yet held).
    Nothing here grants permission; it only records what is held."""
    root = Path(root)
    usage_path = root / 'configs/dataset_study_usage.json'
    reviews_path = root / 'configs/dataset_download_reviews.json'
    if not usage_path.exists() or not reviews_path.exists():
        return []
    usage = read(usage_path)
    reviews = {r['id']: r for r in read(reviews_path).get('reviews', [])}
    lock_path = root / 'configs/research_resources.lock.json'
    locked = {f['id']: f for f in read(lock_path)['files']} if lock_path.exists() else {}
    manifest = {r['id']: r for r in read(root / 'configs/research_resources.json')['resources']} if (root / 'configs/research_resources.json').exists() else {}
    changed = []
    for d in usage['datasets']:
        review = reviews.get(d['id'])
        if d['id'] not in dataset_ids or review is None:
            continue
        aliases = review.get('resource_aliases', [])
        held = [locked[a] for a in aliases if a in locked]
        if not held:
            continue
        full = review.get('full_release') or {}
        folders = sorted({'data/acquisitions/' + '/'.join(f['path'].split('/')[:2]) + '/' for f in held})
        pending = [a for a in aliases if a not in locked]
        missing = []
        if full.get('unbound_files'):
            missing.append(f"{full['unbound_files']} release files have no covering licence record and were not registered.")
        if full.get('unacknowledged_files'):
            missing.append(f"{full['unacknowledged_files']} release files need a technical-risk acknowledgement (no provider checksum or size).")
        if full.get('unsupported_sources'):
            missing.append(f"{len(full['unsupported_sources'])} source URLs have no supported route: " + ', '.join(full['unsupported_sources']) + '.')
        if pending:
            missing.append(f"{len(pending)} registered files are not held yet (quarantined pending release, or failed transfer).")
        # Release files held: what the last resolution found held anywhere (this dataset's folder or another's, e.g. an OpenML
        # set shared with another entry) plus the jobs it registered, minus those withdrawn or still pending; never above the release.
        counted = full['previously_held'] + full.get('jobs_added', 0) - full.get('withdrawn_unheld_jobs', 0) - len(pending) if 'previously_held' in full else len(held)
        held_total = min(full['files'], counted) if full.get('files') else counted
        complete = not missing and (full.get('files') is None or held_total >= full['files'])
        unresolved = sum(1 for a in aliases if a in locked and manifest.get(a, {}).get('kind') != 'repository_source' and not any(
            manifest.get(a, {}).get(k) for k in ('provider_sha256', 'provider_md5', 'git_blob_sha1')))
        d['data_caveats'] = data_caveats(d, aliases, held, manifest, full, root)
        d.update(paths=sorted(set(d.get('paths', [])) | set(folders)), audit_state='acquired_locked_verified',
                 file_integrity='provider_checksums_verified' if not unresolved else 'provider_identities_where_published_local_sha256_otherwise',
                 completeness='complete_public_release' if complete else f"partial_release_{held_total}_of_{full.get('files') or len(aliases)}_files_held",
                 missing_components=missing,
                 next_action=(f"Complete release held ({held_total} files, verified and locked)." if complete else
                              f"{held_total} of {full.get('files') or len(aliases)} release files held; see the missing parts.")
                 + ' AI use needs the source-bound preflight; see the licence records on the dataset page.')
        changed.append(d['id'])
    if changed:
        usage['reviewed_on'] = datetime.now(timezone.utc).date().isoformat()
        write_json(usage_path, usage)
    return changed


def data_caveats(d, aliases, held, manifest, full, root):
    """What is uncertain about the held files, where to look and what to check; computed from the registries, never invented."""
    reviews_path = Path(root) / 'configs/dataset_download_reviews.json'
    approvals = {a['id']: a for a in read(reviews_path).get('approved_artifacts', [])} if reviews_path.exists() else {}
    held_ids = {f['id'] for f in held}
    no_identity = [a for a in aliases if a in held_ids and not any(manifest.get(a, {}).get(k) for k in ('provider_sha256', 'provider_md5', 'git_blob_sha1'))]
    no_size = [a for a in aliases if a in held_ids and 'expected_bytes' not in manifest.get(a, {})]
    unresolved = [a for a in aliases if a in held_ids and approvals.get(a, {}).get('rights_unresolved_download') is True]
    caveats = []
    if no_identity:
        caveats.append(f"Identity: {len(no_identity)} held file(s) have no provider checksum; their identity is the local SHA-256 recorded at first download (lock entries with authority 'local_first_acquisition_SHA256_not_provider_checksum'). "
                       "Check: compare with any checksum the provider later publishes, or download once more and compare bytes.")
    if no_size:
        caveats.append(f"Size: {len(no_size)} held file(s) had no advertised size; the transfer was bounded only by the per-artifact cap. Check: the file opens and its structure matches the provider description (validation report, Files section).")
    if unresolved:
        caveats.append(f"Licence: {len(unresolved)} held file(s) were acquired with no licence decision (open access, rights unresolved); no licence record binds them and the preflight blocks their AI use. "
                       "Where to look: the provider pages listed as sources" + (': ' + ', '.join(d.get('source_urls', [])[:4]) if d.get('source_urls') else '') + ". Record the terms in the dataset’s licence review.")
    if full.get('unsupported_sources'):
        caveats.append("Completeness: " + f"{len(full['unsupported_sources'])} source URL(s) could not be fetched automatically (login, portal or cloud-drive hosting); the held files are not the whole release.")
    if full.get('release'):
        caveats.append(f"Version: the files are the provider release resolved on {full.get('requested', 'the request date')} ({full['release']}); a paper may have used another version or split. Expect to re-derive any paper-specific preprocessing.")
    return caveats


def current_listing(root):
    root = Path(root).resolve()
    latest_path = root / LISTING_DIR / 'latest.json'
    if not latest_path.is_file():
        raise ValueError('quarantine_listing_missing_run_list_first')
    latest = read(latest_path)
    listing_path = contained(root, latest['listing'])
    if digest(listing_path) != latest['listing_sha256']:
        raise ValueError('quarantine_listing_changed')
    return latest, read(listing_path)


def validate_request(request, root):
    """Return (latest, listing, items) for a request that matches the current listing and asks only allowed actions."""
    root = Path(root).resolve()
    if request.get('schema_version') != 1 or request.get('kind') != KIND:
        raise ValueError('unsupported_quarantine_release_request')
    latest, listing = current_listing(root)
    if request.get('listing_sha256') != latest['listing_sha256']:
        raise ValueError('quarantine_listing_changed_regenerate_request')
    if request.get('reviewed') is not True:
        raise ValueError('reviewer_confirmation_required')
    study = request.get('research_study')
    if study:
        study = normalize_study(study)
        current = registered_study(root, study['id'])
        if not current or study_fingerprint(current) != study_fingerprint(study):
            raise ValueError('register_current_research_study_before_quarantine_release')
    items = request.get('items')
    if not isinstance(items, list) or not items or len(items) > 10000:
        raise ValueError('invalid_quarantine_release_items')
    by_key = {(i['run'], i.get('id'), i['job_path']): i for i in listing['items']}
    resolved, keys = [], set()
    for item in items:
        if not isinstance(item, dict) or item.get('action') not in ACTIONS:
            raise ValueError('invalid_quarantine_release_action')
        key = (item.get('run'), item.get('id'), item.get('job_path'))
        if key in keys:
            raise ValueError('duplicate_quarantine_release_item')
        keys.add(key)
        listed = by_key.get(key)
        if listed is None:
            raise ValueError('quarantine_item_not_in_listing: ' + str(key))
        if listed['state'] != ACTIONS[item['action']]:
            raise ValueError(f"action_not_allowed_for_state: {item['action']} on {listed['state']} ({listed['job_path']})")
        if item.get('sha256') and item['sha256'] != listed['sha256']:
            raise ValueError('quarantine_item_identity_mismatch')
        resolved.append({**listed, 'action': item['action']})
    return latest, listing, resolved


def second_download(job, destination, journal):
    """Fetch the artifact again with the acquisition flags and return its digest; the copy is deleted afterwards."""
    partial = destination.with_name(destination.name + '.reverify')
    result = subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error', '--proto', '=https', '--proto-redir', '=https',
                             '--connect-timeout', '20', '--max-time', '600', '--max-filesize', str(job.get('max_bytes', 64 * 1024**2)),
                             '--output', str(partial), job['url']], capture_output=True, text=True)
    try:
        if result.returncode:
            raise RuntimeError(f'reverify_curl_exit_{result.returncode}; {result.stderr[-160:]}')
        return digest(partial)
    finally:
        partial.unlink(missing_ok=True)
        journal('second_download_compared', {'id': job['id'], 'url': job['url'], 'curl_exit': result.returncode})


def apply(request, root, execute=False, reverify=False):
    """Validate and, with execute, release or discard the ticked items; every step is journaled under runs/quarantine_reviews/<run>/."""
    root = Path(root).resolve()
    latest, listing, items = validate_request(request, root)
    run_dir = root / 'runs/quarantine_reviews' / stamp()
    run_dir.mkdir(parents=True)
    write_json(run_dir / 'request.json', request)
    shutil.copyfile(root / latest['listing'], run_dir / 'listing.json')
    for source in (Path(__file__), CODE_ROOT / 'Scripts/acquire_research_resources.py'):
        shutil.copyfile(source, run_dir / source.name)
    events = []

    def journal(kind, details):
        event = {'at': now(), 'kind': kind, 'details': details}
        events.append(event)
        with (run_dir / 'operations.jsonl').open('a') as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
    manifest = read(root / 'configs/research_resources.json')
    jobs = {r['id']: r for r in manifest['resources']}
    lock_path = root / 'configs/research_resources.lock.json'
    lock = read(lock_path) if lock_path.exists() else {'schema_version': 1, 'scope': manifest.get('scope'), 'files': []}
    locked = {f['id']: f for f in lock['files']}
    report_path = root / 'data/acquisitions/acquisition-report.json'
    report = read(report_path) if report_path.exists() else {'checked_at': now(), 'manifest_sha256': digest(root / 'configs/research_resources.json'), 'results': [], 'counts': {}}
    assets_path = root / 'configs/data_license_assets.json'
    assets = read(assets_path)
    known_assets = {(a['path'], a['sha256'], a['resource']) for a in assets['files']}
    outcomes = []
    for item in items:
        outcome = {k: item.get(k) for k in ['run', 'id', 'job_path', 'action', 'bytes', 'sha256', 'dataset_ids']}
        staged = contained(root, item['path'])
        try:
            if not staged.is_file() or digest(staged) != item['sha256'] or staged.stat().st_size != item['bytes']:
                raise ValueError('staged_file_changed_since_listing')
            if item['action'] == 'discard_superseded':
                held = [f for f in locked.values() if f['sha256'] == item['sha256']]
                if not held or any(not (root / 'data/acquisitions' / f['path']).is_file() or digest(root / 'data/acquisitions' / f['path']) != item['sha256'] for f in held):
                    raise ValueError('held_copy_missing_or_changed_duplicate_kept')
                outcome.update(held_as=[f['id'] for f in held], status='discard_planned' if not execute else 'discarded_duplicate')
                if execute:
                    staged.unlink()
                    journal('duplicate_discarded', {'path': item['path'], 'sha256': item['sha256'], 'held_as': outcome['held_as']})
                outcomes.append(outcome)
                continue
            job = jobs.get(item['id'])
            if job is None or job.get('path') != item['job_path'] or job.get('url') != item.get('url'):
                raise ValueError('manifest_job_missing_or_identity_changed')
            if item['id'] in locked:
                raise ValueError('job_already_locked')
            inspection = acquisition.validate(staged, job)  # the same format, size and identity checks a direct acquisition runs
            if job.get('provider_sha256') and job['provider_sha256'] != item['sha256']:
                raise ValueError('provider_sha256_mismatch')
            destination = contained(root / 'data/acquisitions', job['path'])
            if destination.exists():
                raise ValueError('destination_exists_review_manually')
            outcome.update(inspection=inspection, destination=str(destination.relative_to(root)), status='release_planned')
            if not execute:
                outcomes.append(outcome)
                continue
            if reverify:
                if second_download(job, run_dir / Path(job['path']).name, journal) != item['sha256']:
                    raise ValueError('second_download_differs_from_staged_bytes')
                outcome['second_download'] = 'identical'
            journal('release_started', {'id': job['id'], 'from': item['path'], 'to': outcome['destination']})
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(destination))
            if digest(destination) != item['sha256']:
                raise ValueError('released_bytes_changed_during_move')
            authority = item.get('hash_authority') or ('provider_SHA256' if job.get('provider_sha256') else 'GitHub_MCP_git_blob' if job.get('git_blob_sha1')
                                                       else 'provider_MD5_plus_local_SHA256' if job.get('provider_md5') else LOCAL_AUTHORITY)
            entry = {'id': job['id'], 'path': job['path'], 'url': job['url'], 'bytes': item['bytes'], 'sha256': item['sha256'], 'hash_authority': authority,
                     'acquired_at': item.get('acquired_at') or now()}
            locked[job['id']] = entry
            lock['files'] = list(locked.values())
            write_json(lock_path, lock)
            report['results'] = [r for r in report['results'] if r.get('id') != job['id']] + [{**entry, 'state': 'downloaded_verified', 'inspection': inspection,
                                                                                                 'released_from_quarantine': {'run': item['run'], 'review_run': run_dir.name, 'at': now(),
                                                                                                                              'technical_risks': item.get('technical_risks', [])}}]
            report['counts'] = dict(Counter(r['state'] for r in report['results']))
            write_json(report_path, report)
            for resource in item.get('license_resources', []):
                key = ('data/acquisitions/' + job['path'], item['sha256'], resource)
                if key not in known_assets:
                    assets['files'].append({'path': key[0], 'sha256': key[1], 'resource': key[2]})
                    known_assets.add(key)
            assets['as_of'] = datetime.now(timezone.utc).date().isoformat()
            write_json(assets_path, assets)
            with (root / 'data/acquisitions/operations.jsonl').open('a') as stream:
                stream.write(json.dumps({'at': now(), 'kind': 'quarantine_release', 'details': {**entry, 'review_run': run_dir.name}}, sort_keys=True) + '\n')
            outcome.update(status='released_locked', hash_authority=authority, license_resources=item.get('license_resources', []))
            journal('release_finished', {'id': job['id'], 'sha256': item['sha256'], 'hash_authority': authority})
        except Exception as error:
            outcome.update(status='blocked', problem=str(error))
            journal('item_blocked', {'id': item.get('id'), 'path': item['path'], 'problem': str(error)})
        outcomes.append(outcome)
    if execute:
        refresh_dataset_entries(root, {d for o in outcomes if o['status'] == 'released_locked' for d in (o.get('dataset_ids') or [])})
        for run_root in {root / QUARANTINE / i['run'] for i in items}:
            for folder in sorted((p for p in run_root.rglob('*') if p.is_dir()), key=lambda p: -len(p.parts)):
                if not any(folder.iterdir()):
                    folder.rmdir()
            if run_root.is_dir() and not any(run_root.iterdir()):
                run_root.rmdir()
        listing_after = inventory(root)
    result = {'schema_version': 1, 'created_at': now(), 'mode': 'execute' if execute else 'dry_run', 'reverify': reverify, 'run': str(run_dir.relative_to(root)),
              'outcomes': outcomes, 'counts': dict(Counter(o['status'] for o in outcomes)), 'quarantine_after': listing_after['summary'] if execute else None,
              'ai_use_authorized_by_release': False, 'model_calls': 0}
    write_json(run_dir / 'report.json', result)
    if execute:
        study = request.get('research_study', {}).get('id') if request.get('research_study') else default_study(root)
        released = [o for o in outcomes if o['status'] == 'released_locked']
        discarded = [o for o in outcomes if o['status'] == 'discarded_duplicate']
        append_log(root, study, [{'at': now(), 'action': 'quarantine_release', 'source': 'review_quarantine.py', 'run': result['run'],
                                  'detail': (f"Released {len(released)} quarantined deliveries into data/acquisitions ({sum(o['bytes'] or 0 for o in released):,} bytes; "
                                             f"{'second download compared' if reverify else 'no second download'}), discarded {len(discarded)} superseded duplicates, "
                                             f"{result['counts'].get('blocked', 0)} blocked. Datasets: {', '.join(sorted({d for o in released + discarded for d in o.get('dataset_ids') or []})) or 'none'}. "
                                             'Releases grant no AI-use permission.')[:1000]}])
    print(json.dumps({k: result[k] for k in ['mode', 'run', 'counts', 'quarantine_after']}))
    return result


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', nargs='?', default='list', choices=['list'])
    parser.add_argument('--request', type=Path, help='A datahunter_quarantine_release request saved by the Quarantine page')
    parser.add_argument('--apply', action='store_true', help='Carry out the validated request; without it the run is a dry validation')
    parser.add_argument('--reverify-download', action='store_true', help='Fetch each released artifact a second time and require identical bytes')
    args = parser.parse_args(argv)
    if args.request:
        return apply(read(args.request), root, execute=args.apply, reverify=args.reverify_download)
    listing = inventory(root)
    print(json.dumps({'items': len(listing['items']), 'summary': listing['summary'], 'bytes': listing['bytes'], 'listing': str(root / LISTING_DIR / 'latest.json')}))
    return listing


if __name__ == '__main__':
    require_workspace(ROOT)
    main()
