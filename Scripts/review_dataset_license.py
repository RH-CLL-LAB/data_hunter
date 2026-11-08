#!/usr/bin/env python3
"""Validate and optionally register a human's source-bound licence review.

The static Explorer saves a decision request; it cannot grant runtime permission.
This importer makes no network/model calls, fetches no data and preserves sealed
before/after evidence. A manual interpretation never overrides an explicit ban.
"""
import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root, require_workspace
ROOT = workspace_root(default=CODE_ROOT)
from datahunter.data_rights import LicenseRegistry, OPERATIONS, _read
from datahunter.research_studies import default_study, normalize as normalize_study, registered as registered_study, fingerprint as study_fingerprint, append_log as append_study_log

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def contained(root, value):
    p = root / value
    if Path(value).is_absolute() or '..' in Path(value).parts or p.is_symlink() or not p.resolve().is_relative_to(root):
        raise ValueError('project_relative_regular_file_required')
    if not p.is_file():
        raise ValueError('review_evidence_or_asset_missing: ' + value)
    return p

def text(request, key):
    value = request.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError('missing_or_invalid_' + key)
    return value.strip()

def validate(request, root=ROOT):
    root = Path(root).resolve()
    if request.get('schema_version') != 1 or request.get('kind') not in ('datahunter_license_review', 'medscrape_license_review'):
        raise ValueError('unsupported_license_review')
    receipt = _read(root / 'data/interim/dataset_selection/latest.json')
    catalogue = contained(root, receipt['selection'])
    if request.get('catalogue_sha256') != digest(catalogue):
        raise ValueError('stale_catalogue_review')
    policy_path, assets_path = [root / 'configs' / p for p in ['data_licenses.json', 'data_license_assets.json']]
    if request.get('policy_sha256') != digest(policy_path) or request.get('assets_sha256') != digest(assets_path):
        raise ValueError('stale_permission_register_review')
    policy, assets = _read(policy_path), _read(assets_path)
    profile = request.get('research_study')
    if profile:
        profile = normalize_study(profile)
        actual = registered_study(root, profile['id'])
        if not actual or study_fingerprint(actual) != study_fingerprint(profile):
            raise ValueError('register_current_research_study_profile_before_licence_review')
    datasets = {d['id']: d for d in _read(catalogue)['datasets']}
    dataset_id = request.get('dataset_id')
    if dataset_id not in datasets:
        raise ValueError('unknown_catalogue_id')
    for key in ['reviewer', 'purpose', 'source_version', 'scope', 'rationale', 'license_name']:
        text(request, key)
    today = date.today()
    reviewed, due = [date.fromisoformat(text(request, k)) for k in ['reviewed_on', 'review_due']]
    if reviewed > today or due < today or due < reviewed:
        raise ValueError('review_date_missing_future_or_expired')
    verdict = request.get('verdict')
    if verdict not in {'approve', 'needs_clarification', 'prohibited'}:
        raise ValueError('invalid_review_verdict')
    ops = request.get('operations')
    if not isinstance(ops, list) or not ops or len(set(ops)) != len(ops) or not set(ops) <= OPERATIONS:
        raise ValueError('specific_review_operations_required')
    if request.get('ai_evidence') not in {'silent', 'explicit_approval', 'explicit_prohibition'}:
        raise ValueError('invalid_ai_evidence_state')
    if request['ai_evidence'] != 'silent':
        text(request, 'ai_evidence_note')
    if verdict == 'approve':
        if request['ai_evidence'] == 'explicit_prohibition':
            raise ValueError('cannot_approve_explicit_ai_prohibition')
        for key in ['underlying_rights_confirmed', 'conditions_satisfied', 'agreements_complete', 'whole_artifacts_reviewed']:
            if request.get(key) is not True:
                raise ValueError('approval_requires_' + key)
    conditions = request.get('conditions')
    if not isinstance(conditions, list) or any(not isinstance(c, str) or not c.strip() for c in conditions):
        raise ValueError('conditions_must_be_a_list_of_text')
    evidence = request.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        raise ValueError('retained_license_evidence_required')
    for item in evidence:
        path = contained(root, item['snapshot'])
        if path.stat().st_size == 0 or digest(path) != item.get('sha256'):
            raise ValueError('license_evidence_empty_or_changed')
        if not re.match(r'^https://[^/\s]+', item.get('url', '')):
            raise ValueError('official_license_source_url_required')
    # Only the current catalogue's own retained/prepared files can be selected.
    d = datasets[dataset_id]
    candidate_paths = {f['path'] for key in ['obtained_artifacts', 'prepared_artifact_profiles'] for f in d.get(key, [])}
    selected = request.get('assets')
    if not isinstance(selected, list):
        raise ValueError('exact_assets_list_required')
    seen = set()
    for item in selected:
        path = item.get('path')
        if path not in candidate_paths or path in seen:
            raise ValueError('asset_outside_dataset_or_duplicate')
        seen.add(path)
        if digest(contained(root, path)) != item.get('sha256'):
            raise ValueError('reviewed_asset_changed')
    fingerprints = {a['sha256'] for a in selected}
    if verdict != 'approve' and not selected:
        # Source-only review cannot silently shadow an existing exact-file grant.
        registry = LicenseRegistry(policy_path, assets_path, root=root, mode='lenient', study_id=profile['id'] if profile else default_study(root))
        existing = [d.get('associated_download_permissions', {})] + [s['permission_preflight'] for s in d.get('input_scopes', [])]
        hashes = {h for report in existing for h in report.get('source_sha256', [])}
        if any(registry.check([h], ops)['status'] == 'allowed' for h in hashes):
            raise ValueError('select_exact_held_files_to_restrict_existing_clearance')
    affected = {a['resource'] for a in assets['files'] if a['sha256'] in fingerprints}
    resources = {r['id']: r for r in policy['resources']}
    if verdict == 'approve':
        review_study_id = profile['id'] if profile else default_study(root)
        source_ids = {r['id'] for r in policy['resources'] if dataset_id in r.get('catalog_ids', []) and r.get('scope') != 'software_only'}
        source_ids.update(v.get('resource') for v in d.get('associated_download_permissions', {}).get('decisions', []) if v.get('resource') in resources)
        for source_id in source_ids - affected:
            source = resources[source_id]
            if any(source.get('ai_permissions', {}).get(op) == 'prohibited' for op in ops):
                raise ValueError('cannot_override_known_source_ai_prohibition: ' + source_id)
            if source.get('study_id') and source['study_id'] != review_study_id:
                continue
            if any(source['operations'].get(op) == 'prohibited'
                   or any(op in p.get('operations', []) for p in source.get('conditional_prohibitions', [])) for op in ops):
                raise ValueError('cannot_override_known_source_prohibition_before_asset_registration: ' + source_id)
        for resource in affected:
            r = resources[resource]
            if profile and profile['id'] != default_study(root):
                prior_review = r.get('study_reviews', {}).get(profile['id'], {})
                if any(prior_review.get('operations', {}).get(op) == 'prohibited' or prior_review.get('ai_permissions', {}).get(op) == 'prohibited' for op in ops):
                    raise ValueError('cannot_override_existing_study_prohibition: ' + resource)
            # Do not broaden an existing resource's decision to unreviewed files.
            covered = {a['sha256'] for a in assets['files'] if a['resource'] == resource}
            if not covered <= fingerprints:
                raise ValueError('partial_resource_review_requires_separate_scoped_registration: ' + resource)
            if any(r['operations'].get(op) == 'prohibited' or r.get('ai_permissions', {}).get(op) == 'prohibited'
                   or any(op in p.get('operations', []) for p in r.get('conditional_prohibitions', [])) for op in ops):
                raise ValueError('cannot_override_existing_prohibition: ' + resource)
            if r.get('scope') == 'software_only':
                raise ValueError('software_permission_cannot_be_promoted_to_data_rights')
            if any(state == 'allowed' and op not in ops for op, state in r['operations'].items()):
                raise ValueError('existing_allowed_operations_require_review_before_refreshing_resource: ' + resource)
    return policy, assets, affected

def review(path, apply=False, root=ROOT):
    root = Path(root).resolve()
    request = _read(path)
    policy, assets, affected = validate(request, root)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    run = root / 'runs/license_reviews' / stamp
    run.mkdir(parents=True)
    def save(name, value):
        (run / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    save('request.json', request)
    shutil.copyfile(__file__, run / 'review-importer.py')
    role = CODE_ROOT / 'agents/dataset-license-review-agent.md'
    if role.is_file():
        shutil.copyfile(role, run / 'agent-definition.md')
    save('policy-before.json', policy)
    save('assets-before.json', assets)
    selected = request['assets']
    profile = normalize_study(request['research_study']) if request.get('research_study') else None
    study_id = profile['id'] if profile else default_study(root)
    study_hash = study_fingerprint(profile) if profile else None
    resource_id = 'manual_' + request['dataset_id'].lower() + '_' + stamp.lower()
    status = {'approve': 'approved_source_pending_identity', 'needs_clarification': 'needs_manual_review', 'prohibited': 'prohibited'}[request['verdict']]
    result = {'schema_version': 1, 'dataset_id': request['dataset_id'], 'status': 'validated_pending_registration', 'applied': False,
              'run': str(run.relative_to(root)), 'reviewer': request['reviewer'], 'review_due': request['review_due'],
              'operations': request['operations'], 'assets': selected, 'request_sha256': digest(run / 'request.json'),
              'study_id': study_id, 'study_sha256': study_hash, 'model_calls': 0, 'download_bytes': 0}
    if apply:
        new_policy, new_assets = deepcopy(policy), deepcopy(assets)
        # Source review before acquisition can create an operation-scoped resource;
        # no input is cleared until its exact delivery identity is registered.
        if request['verdict'] == 'approve':
            conditions = list(dict.fromkeys(request['conditions'] + [c for r in policy['resources'] if r['id'] in affected for c in r.get('conditions', [])]))
            record = {'id': resource_id, 'name': request['dataset_id'] + ' — manually reviewed exact scope',
                      'study_id': study_id, 'study_sha256': study_hash,
                      'catalog_ids': [request['dataset_id']], 'license': request['license_name'],
                      'scope': request['scope'], 'source_version': request['source_version'],
                      'reviewed_on': request['reviewed_on'], 'review_due': request['review_due'],
                      'reason': request['rationale'], 'conditions': conditions,
                      'sources': [e['url'] for e in request['evidence']],
                      'evidence': [{'snapshot': e['snapshot'], 'sha256': e['sha256']} for e in request['evidence']],
                      'operations': {op: 'allowed' if op in request['operations'] else 'unreviewed' for op in OPERATIONS},
                      'ai_permissions': {op: 'allowed' if op in request['operations'] and request['ai_evidence'] == 'explicit_approval' else 'unreviewed' for op in OPERATIONS if op != 'redistribution'},
                      'manual_review': {'reviewer': request['reviewer'], 'purpose': request['purpose'], 'study_id': study_id, 'study_sha256': study_hash, 'ai_evidence_note': request.get('ai_evidence_note', ''), 'request': str((run / 'request.json').relative_to(root)), 'request_sha256': result['request_sha256']}}
            if affected:
                # Full resource scope was required above. Preserve other operations
                # rather than silently revoking or granting unrelated uses.
                for r in new_policy['resources']:
                    if r['id'] not in affected:
                        continue
                    if study_id != default_study(root):
                        r.setdefault('study_reviews', {})[study_id] = record
                        continue
                    r.update({k: record[k] for k in ['license', 'scope', 'source_version', 'sources', 'reviewed_on', 'review_due', 'evidence', 'manual_review']})
                    r['conditions'] = conditions
                    for op in request['operations']:
                        r['operations'][op] = 'allowed'
                        if op != 'redistribution':
                            r.setdefault('ai_permissions', {})[op] = record['ai_permissions'][op]
            known = {a['path'] for a in new_assets['files']}
            unregistered = [a for a in selected if a['path'] not in known]
            if unregistered or not selected:
                new_policy['resources'].append(record)
                for a in unregistered:
                    new_assets['files'].append({**a, 'resource': resource_id})
            # Refuse to claim approval if any remaining matching component blocks.
            save('policy-proposed.json', new_policy)
            save('assets-proposed.json', new_assets)
            registry = LicenseRegistry(run / 'policy-proposed.json', run / 'assets-proposed.json', root=root, mode='lenient', study_id=study_id)
            if selected:
                preflight = registry.check([a['sha256'] for a in selected], request['operations'])
                save('preflight.json', preflight)
                if preflight['status'] != 'allowed':
                    save('report.json', {**result, 'status': 'blocked_all_component_preflight'})
                    raise ValueError('manual_review_does_not_clear_all_applicable_components: ' + str(run))
                status = 'approved_exact_assets'
            for name, document in [('data_licenses.json', new_policy), ('data_license_assets.json', new_assets)]:
                target = root / 'configs' / name
                temporary = target.with_suffix('.review.tmp')
                temporary.write_text(json.dumps(document, indent=2) + '\n')
                temporary.replace(target)
            result['resource_id'] = resource_id if unregistered or not selected else sorted(affected)
        else:
            # A recorded concern must not leave an old AI clearance active.
            state = 'prohibited' if request['verdict'] == 'prohibited' else 'unreviewed'
            for r in new_policy['resources']:
                if r['id'] not in affected:
                    continue
                if study_id != default_study(root):
                    prior = r.setdefault('study_reviews', {}).get(study_id, {})
                    r['study_reviews'][study_id] = {'study_id': study_id, 'study_sha256': study_hash,
                        'operations': {op: 'prohibited' if prior.get('operations', {}).get(op) == 'prohibited' else state if op in request['operations'] else 'unreviewed' for op in OPERATIONS},
                        'ai_permissions': {op: 'prohibited' if request['ai_evidence'] == 'explicit_prohibition' and op in request['operations'] else 'unreviewed' for op in OPERATIONS if op != 'redistribution'},
                        'reason': request['rationale'], 'reviewed_on': request['reviewed_on'], 'review_due': request['review_due'],
                        'evidence': request['evidence'], 'license': request['license_name'], 'conditions': request['conditions']}
                    continue
                for op in request['operations']:
                    if r['operations'].get(op) != 'prohibited':
                        r['operations'][op] = state
                r['reason'] = 'Manual review: ' + request['rationale']
            unknown = [a for a in selected if a['path'] not in {x['path'] for x in new_assets['files']}]
            if unknown or not selected:
                new_policy['resources'].append({'id': resource_id, 'name': request['dataset_id'] + ' — manual restriction',
                    'study_id': study_id, 'study_sha256': study_hash,
                    'catalog_ids': [request['dataset_id']], 'license': request['license_name'], 'scope': request['scope'],
                    'reason': request['rationale'], 'reviewed_on': request['reviewed_on'], 'review_due': request['review_due'],
                    'conditions': request['conditions'], 'sources': [e['url'] for e in request['evidence']],
                    'evidence': [{'snapshot': e['snapshot'], 'sha256': e['sha256']} for e in request['evidence']],
                    'operations': {op: state if op in request['operations'] else 'unreviewed' for op in OPERATIONS},
                    'ai_permissions': {op: 'prohibited' if op in request['operations'] and request['ai_evidence'] == 'explicit_prohibition' else 'unreviewed' for op in OPERATIONS if op != 'redistribution'}})
                new_assets['files'].extend({**a, 'resource': resource_id} for a in unknown)
            policy_target = root / 'configs/data_licenses.json'
            policy_target.write_text(json.dumps(new_policy, indent=2) + '\n')
            (root / 'configs/data_license_assets.json').write_text(json.dumps(new_assets, indent=2) + '\n')
        history_path = root / 'configs/dataset_license_reviews.json'
        history = _read(history_path) if history_path.exists() else {'schema_version': 1, 'reviews': []}
        result.update(status=status, applied=True)
        history['reviews'].append({**result, 'reviewed_on': request['reviewed_on'], 'source_version': request['source_version'], 'ai_evidence': request['ai_evidence'], 'evidence': request['evidence']})
        prior_reviews = [h for h in history['reviews'][:-1] if h.get('dataset_id') == request['dataset_id'] and h.get('study_id', default_study(root)) == study_id]
        history_path.write_text(json.dumps(history, indent=2) + '\n')
        # Study log: every registered licence decision, marking revisions of an earlier decision.
        append_study_log(root, study_id, [{'at': datetime.now(timezone.utc).isoformat(), 'action': 'licence_review_registered',
            'source': 'review_dataset_license.py', 'run': str(run.relative_to(root)),
            'detail': (f"{request['dataset_id']}: {request['verdict']} → {status}; operations {', '.join(request['operations'])}; "
                       f"AI evidence {request['ai_evidence']}; reviewer {request['reviewer']}; review due {request['review_due']}"
                       + (f"; revises {len(prior_reviews)} earlier decision(s)" if prior_reviews else '; first decision for this study'))[:1000]}])
    save('report.json', result)
    save('policy-after.json', _read(root / 'configs/data_licenses.json'))
    save('assets-after.json', _read(root / 'configs/data_license_assets.json'))
    (run / 'operations.jsonl').write_text(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'kind': 'manual_license_review', 'details': result}) + '\n')
    save('completion.json', {'completed_at': datetime.now(timezone.utc).isoformat(), 'files': [
        {'path': str(p.relative_to(run)), 'sha256': digest(p)} for p in sorted(run.rglob('*')) if p.is_file()]})
    print(json.dumps(result, indent=2))
    return result

if __name__ == '__main__':
    require_workspace(ROOT)  # a clear message instead of a missing-file error when no study is active
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='Record the supplied human decision after validation.')
    args = parser.parse_args()
    review(args.request, args.apply)
