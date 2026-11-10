#!/usr/bin/env python3
"""Copy the shareable, non-file facts of one study workspace into another.

    python Scripts/merge_study_facts.py --source harmonization-test                  # dry run into the active study
    python Scripts/merge_study_facts.py --source harmonization-test --target my-study --apply

What crosses over, and how:
- catalogue entries (configs/dataset_study_usage.json, `datasets`) the target lacks, added as pre-access entries:
  paths, audit state, verified artifacts, completeness, integrity and caveats are reset, because the target holds
  nothing until it acquires or reuses files; an existing entry is never touched, and an existing entry whose facts
  differ is reported as a conflict;
- paper records (`studies`) the target lacks;
- selection-policy placements the target lacks, for datasets in its catalogue: priority groups and relevance
  overrides (dataset_selection.json), category groups, families with their notes and review notes
  (dataset_validation.json);
- pre-access terms screens (dataset_selection.json, preaccess_license_policy.reviews) the target lacks, for datasets
  no target screen covers, with `rights_granted` always false, and the registration census of the added entries;
- retained notices under data/raw/**/notices/ and the evidence files of the copied records, copied by hash; a path
  the target already holds with other bytes is a conflict and is refused;
- licence records (data_licenses.json) the target lacks, re-bound to the target study with every operation set to
  unreviewed (a prohibition stays a prohibition; an allowance never crosses), manual decisions and study overlays
  dropped, `catalog_ids` kept. A record or screen whose evidence is missing, changed or conflicting is not copied.

Never merged: research briefs, shortlists and dataset/paper annotations (one study's relevance), licence assets and
acquisition locks (bound to held files), download reviews and acquisition jobs, manual licence decisions. Copying a
record grants nothing: the target study's own licence review and the source-bound preflight (DATA_HUNTER_STUDY_ID)
still decide every AI operation.

Dry run by default: the plan is written to the target's runs/merge_study_facts/<stamp>/ and printed. With --apply the
registries and files are written and a note is appended to both studies' actions.jsonl.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import valid_id, workspace_root, require_workspace, study_path, is_study_workspace, workspace_study_id, STUDIES  # noqa: E402
from datahunter.research_studies import append_log, registered, fingerprint  # noqa: E402
ROOT = workspace_root(default=CODE_ROOT)

OPERATIONS = ('local_inference', 'local_embedding', 'remote_inference', 'remote_embedding', 'training', 'redistribution')
# What a study holds is never a fact about the dataset: a copied entry starts from nothing.
HELD_STATE = {'paths': [], 'audit_state': 'pre_access_registered', 'verified_artifacts': [], 'completeness': 'not_obtained',
              'file_integrity': 'not_checked', 'full_paper_reproduction_ready': False}
HELD_STATE_DROPPED = ('data_caveats', 'associated_download_files', 'associated_download_permissions')
HELD_STATE_TEXT = ('missing_components', 'next_action')
PER_STUDY_LICENCE_KEYS = ('manual_review', 'study_reviews')
REGISTRIES = ('dataset_study_usage.json', 'dataset_selection.json', 'dataset_validation.json', 'data_licenses.json')
NOT_MERGED = {
    'research brief, shortlist and dataset/paper annotations': 'they express one study’s relevance; the target annotates from its own search',
    'licence assets and acquisition locks': 'they bind held files; the target holds nothing until it acquires or reuses them',
    'download reviews, acquisition jobs and resource scope': 'acquisition state is produced per study by the resolver',
    'manual licence decisions and study overlays': 'licence decisions are per study and never inherited',
}


REQUEST_KIND = 'datahunter_study_facts_merge_request'  # saved by the Research studies page's Merge control


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def workspace(name, default=None):
    """A registered study workspace from a study id under studies/ or a folder path."""
    if name is None:
        folder = Path(default)
    else:
        try:
            folder = Path(name) if Path(name).is_dir() else study_path(name)
        except ValueError as error:
            raise SystemExit(str(error) + ': ' + str(name))
    folder = folder.resolve()
    if not is_study_workspace(folder):
        raise SystemExit('not_a_study_workspace: ' + str(folder))
    study_id = workspace_study_id(folder)
    if not study_id or not registered(folder, study_id):
        raise SystemExit('register_the_research_study_first: ' + str(folder))
    return folder, study_id


def is_held(entry):
    return bool(entry.get('paths')) or bool(entry.get('verified_artifacts')) or entry.get('audit_state') == 'acquired_locked_verified' \
        or str(entry.get('completeness', '')).startswith(('complete', 'partial'))


def preaccess_copy(entry, source_id):
    """The entry as the target may hold it: the facts, with the held state reset to nothing."""
    copy = {k: deepcopy(v) for k, v in entry.items() if k not in HELD_STATE_DROPPED}
    copy.update(deepcopy(HELD_STATE))
    if is_held(entry):  # the source's text describes its own holdings
        copy['missing_components'] = [f'Files not acquired or reused by this study; {source_id} holds them, which is no permission here.']
        copy['next_action'] = (f'Not held by this study. Acquire the release with resolve_full_release.py, or copy the files {source_id} holds with '
                               f'--reuse-from-study {source_id}; the licence review and the source-bound preflight then run for this study.')
    return copy


def fact_keys(*entries):
    skip = set(HELD_STATE) | set(HELD_STATE_DROPPED) | set(HELD_STATE_TEXT)
    return sorted({k for entry in entries for k in entry} - skip)


def plan_catalogue(source_usage, target_usage, source_id):
    existing = {d['id']: d for d in target_usage['datasets']}
    datasets = []
    for entry in source_usage['datasets']:
        other = existing.get(entry['id'])
        if other is None:
            datasets.append({'id': entry['id'], 'state': 'add', 'held_in_source': is_held(entry), 'entry': preaccess_copy(entry, source_id)})
        else:
            differs = [k for k in fact_keys(entry, other) if entry.get(k) != other.get(k)]
            datasets.append({'id': entry['id'], 'state': 'conflict' if differs else 'present', 'differs_in': differs})
    known = {s['id']: s for s in target_usage['studies']}
    papers = []
    for record in source_usage['studies']:
        other = known.get(record['id'])
        if other is None:
            papers.append({'id': record['id'], 'state': 'add', 'record': deepcopy(record)})
        else:
            differs = sorted(k for k in set(record) | set(other) if record.get(k) != other.get(k))
            papers.append({'id': record['id'], 'state': 'conflict' if differs else 'present', 'differs_in': differs})
    return datasets, papers


def file_state(source, target, rel):
    """How one retained file crosses: copy, present (same bytes already there) or conflict (other bytes there)."""
    origin, destination = source / rel, target / rel
    if origin.is_symlink() or not origin.is_file():
        return {'path': rel, 'state': 'missing', 'reason': 'not_a_regular_file_in_source'}
    sha = digest(origin)
    if destination.is_symlink() or destination.exists() and not destination.is_file():
        return {'path': rel, 'sha256': sha, 'state': 'conflict', 'reason': 'target_path_is_not_a_regular_file'}
    if destination.is_file():
        same = digest(destination) == sha
        return {'path': rel, 'sha256': sha, 'state': 'present' if same else 'conflict', **({} if same else {'reason': 'target_holds_other_bytes'})}
    return {'path': rel, 'sha256': sha, 'state': 'copy'}


def plan_notices(source, target):
    base = source / 'data/raw'
    files = []
    if not base.is_dir():
        return files
    for folder in sorted(p for p in base.rglob('notices') if p.is_dir() and not p.is_symlink()):
        for path in sorted(p for p in folder.rglob('*') if p.is_file() or p.is_symlink()):
            files.append({**file_state(source, target, path.relative_to(source).as_posix()), 'kind': 'notice'})
    return files


class Evidence:
    """Evidence files of copied records: notices already planned, other files added to the plan on first use."""

    def __init__(self, files, source, target):
        self.files, self.source, self.target = files, source, target
        self.by_path = {f['path']: f for f in files}

    def problem(self, rel, sha):
        if rel not in self.by_path:
            if rel.startswith('data/acquisitions/'):
                self.by_path[rel] = {'path': rel, 'state': 'held_file', 'reason': 'evidence_is_a_held_file_acquire_or_reuse_it_first', 'kind': 'evidence'}
            else:
                self.by_path[rel] = {**file_state(self.source, self.target, rel), 'kind': 'evidence'}
            self.files.append(self.by_path[rel])
        state = self.by_path[rel]
        if state['state'] == 'held_file':
            return rel + ': ' + state['reason']
        if state['state'] == 'missing' or state.get('sha256') != sha:
            return rel + ': evidence_missing_or_changed_in_source'
        if state['state'] == 'conflict':
            return rel + ': evidence_conflicts_with_target_file'
        return None


def rebind(record, source_id, target_id, target_hash, run, at):
    """The record for the target study: facts and evidence kept, every operation unreviewed, decisions dropped."""
    copy = {k: deepcopy(v) for k, v in record.items() if k not in PER_STUDY_LICENCE_KEYS}
    copy['study_id'], copy['study_sha256'] = target_id, target_hash
    copy['operations'] = {op: 'prohibited' if record.get('operations', {}).get(op) == 'prohibited' else 'unreviewed' for op in OPERATIONS}
    copy['ai_permissions'] = {op: 'prohibited' for op, state in record.get('ai_permissions', {}).items() if state == 'prohibited'}
    copy['reason'] = 'copied_from_another_study_review_required'
    copy['merged_from'] = {'study': source_id, 'reason': record.get('reason'), 'run': run, 'at': at}
    return copy


def plan_licences(source_policy, target_policy, evidence, source_id, target_id, target_hash, run, at):
    existing = {r['id']: r for r in target_policy['resources']}
    licences = []
    for record in source_policy['resources']:
        other = existing.get(record['id'])
        if other is not None:
            keys = ('license', 'scope', 'sources', 'evidence', 'catalog_ids', 'covers_urls', 'source_version', 'conditional_prohibitions')
            differs = [k for k in keys if record.get(k) != other.get(k)]
            licences.append({'id': record['id'], 'state': 'conflict' if differs else 'present', 'differs_in': differs})
            continue
        reasons = [p for p in (evidence.problem(item['snapshot'], item['sha256']) for item in record.get('evidence', [])) if p]
        if reasons:
            licences.append({'id': record['id'], 'state': 'blocked', 'reasons': reasons})
        else:
            licences.append({'id': record['id'], 'state': 'add', 'record': rebind(record, source_id, target_id, target_hash, run, at)})
    return licences


def plan_policy(source_selection, target_selection, source_validation, target_validation, catalogue_ids):
    """Placements the target lacks for datasets of its (merged) catalogue; a target placement always wins."""
    placed = {i for g in target_selection.get('priority_groups', []) for i in g['ids']}
    priority = []
    for group in source_selection.get('priority_groups', []):
        ids = [i for i in group['ids'] if i in catalogue_ids and i not in placed]
        if ids:
            placed.update(ids)
            known = any(g['priority'] == group['priority'] for g in target_selection.get('priority_groups', []))
            priority.append({'priority': group['priority'], 'ids': ids, 'create': not known, **({'action': group.get('action', '')} if not known else {})})
    relevance = {i: deepcopy(v) for i, v in source_selection.get('relevance_overrides', {}).items()
                 if i in catalogue_ids and i not in target_selection.get('relevance_overrides', {})}
    grouped = {i for ids in target_validation.get('groups', {}).values() for i in ids}
    groups = {}
    for category, ids in source_validation.get('groups', {}).items():
        fresh = [i for i in ids if i in catalogue_ids and i not in grouped]
        if fresh:
            grouped.update(fresh)
            groups[category] = fresh
    families, notes = {}, {}
    for family, ids in source_validation.get('families', {}).items():
        fresh = [i for i in ids if i in catalogue_ids and i not in target_validation.get('families', {}).get(family, [])]
        if fresh:
            families[family] = fresh
            if family not in target_validation.get('shared_family_notes', {}) and family in source_validation.get('shared_family_notes', {}):
                notes[family] = source_validation['shared_family_notes'][family]
    review_notes = {i: n for i, n in source_validation.get('review_notes', {}).items()
                    if i in catalogue_ids and i not in target_validation.get('review_notes', {})}
    return {'priority_groups': priority, 'relevance_overrides': relevance, 'groups': groups, 'families': families,
            'shared_family_notes': notes, 'review_notes': review_notes}


def plan_preaccess(source_selection, target_selection, placements, added, catalogue_ids, evidence, source_id, run):
    """Terms screens the target lacks (rights never granted) and the registration census of the added entries."""
    source_pre, target_pre = source_selection.get('preaccess_license_policy', {}), target_selection.get('preaccess_license_policy', {})
    known = {r['id'] for r in target_pre.get('reviews', [])}
    covered = {i for r in target_pre.get('reviews', []) for i in r['dataset_ids']}
    screens = []
    for review in source_pre.get('reviews', []):
        ids = [i for i in review['dataset_ids'] if i in catalogue_ids and i not in covered]
        if not ids:
            screens.append({'id': review['id'], 'state': 'present' if review['id'] in known else 'skipped', **({} if review['id'] in known else {'reason': 'its datasets are absent from the catalogue or screened already'})})
            continue
        reasons = [p for p in (evidence.problem(item['path'], item['sha256']) for item in review.get('evidence', [])) if p]
        if reasons:
            screens.append({'id': review['id'], 'state': 'blocked', 'reasons': reasons})
            continue
        # A screen whose id the target already uses keeps the target's version; its datasets the target has not screened get the source's reading under a derived id.
        new_id = review['id'] if review['id'] not in known else f"{review['id']}__{re.sub(r'[^a-z0-9_]+', '_', source_id.lower())}"
        if new_id in known:
            screens.append({'id': review['id'], 'state': 'skipped', 'reason': f'{new_id} exists already; screen {ids} for this study'})
            continue
        known.add(new_id)
        covered.update(ids)
        copy = {**deepcopy(review), 'id': new_id, 'dataset_ids': ids, 'rights_granted': False, 'merged_from': {'study': source_id, 'run': run, 'screen': review['id']}}
        screens.append({'id': new_id, 'state': 'add', 'review': copy, **({'renamed_from': review['id']} if new_id != review['id'] else {})})
    # The census the documentation gate checks: registration-route entries by the priority they land on (mirrors judgement()).
    census = {'required_registration_ids': [], 'deferred_registration_ids': []}
    coverage = target_pre.get('coverage')
    if coverage:
        priorities = {i: g['priority'] for g in target_selection.get('priority_groups', []) for i in g['ids']}
        priorities.update({i: g['priority'] for g in placements['priority_groups'] for i in g['ids']})
        overrides = {**target_selection.get('relevance_overrides', {}), **placements['relevance_overrides']}
        listed = set(coverage.get('required_registration_ids', [])) | set(coverage.get('deferred_registration_ids', []))
        for entry in added:
            route = entry.get('access_route', '')
            if entry['id'] in listed or 'registration' not in route or 'no_registration' in route:
                continue
            priority = priorities.get(entry['id']) or overrides.get(entry['id'], {}).get('action_priority') or target_selection.get('default', {}).get('action_priority')
            if priority in {'P1', 'P2'}:
                census['required_registration_ids'].append(entry['id'])
            elif priority == 'P3 / hold':
                census['deferred_registration_ids'].append(entry['id'])
    return {'screens': screens, 'census': census}


def apply_plan(report, source, target, usage, selection, validation, policy):
    for f in report['files']:
        if f['state'] != 'copy':
            continue
        destination = target / f['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / f['path'], destination)
        if digest(destination) != f['sha256']:
            raise ValueError('copied_file_hash_mismatch: ' + f['path'])
        f['state'] = 'copied'
    usage['datasets'] += [d['entry'] for d in report['datasets'] if d['state'] == 'add']
    usage['studies'] += [p['record'] for p in report['papers'] if p['state'] == 'add']
    policy['resources'] += [r['record'] for r in report['licences'] if r['state'] == 'add']
    placements = report['policy']
    for group in placements['priority_groups']:
        known = next((g for g in selection.setdefault('priority_groups', []) if g['priority'] == group['priority']), None)
        if known:
            known['ids'] += group['ids']
        else:
            selection['priority_groups'].append({'priority': group['priority'], 'ids': list(group['ids']), 'action': group.get('action', '')})
    selection.setdefault('relevance_overrides', {}).update(placements['relevance_overrides'])
    for category, ids in placements['groups'].items():
        validation.setdefault('groups', {}).setdefault(category, []).extend(ids)
    for family, ids in placements['families'].items():
        validation.setdefault('families', {}).setdefault(family, []).extend(ids)
    validation.setdefault('shared_family_notes', {}).update(placements['shared_family_notes'])
    validation.setdefault('review_notes', {}).update(placements['review_notes'])
    preaccess = report['preaccess']
    if any(s['state'] == 'add' for s in preaccess['screens']):
        selection.setdefault('preaccess_license_policy', {}).setdefault('reviews', []).extend(s['review'] for s in preaccess['screens'] if s['state'] == 'add')
    coverage = selection.get('preaccess_license_policy', {}).get('coverage')
    if coverage:
        for key, ids in preaccess['census'].items():
            coverage.setdefault(key, []).extend(ids)
    write(target / 'configs/dataset_study_usage.json', usage)
    write(target / 'configs/dataset_selection.json', selection)
    write(target / 'configs/dataset_validation.json', validation)
    write(target / 'configs/data_licenses.json', policy)


def counts_of(report):
    def tally(items, *states):
        return {s: sum(1 for i in items if i['state'] == s) for s in states}
    policy, preaccess = report['policy'], report['preaccess']
    return {'datasets': tally(report['datasets'], 'add', 'present', 'conflict'),
            'papers': tally(report['papers'], 'add', 'present', 'conflict'),
            'files': tally(report['files'], 'copy', 'copied', 'present', 'conflict', 'missing', 'held_file'),
            'licences': tally(report['licences'], 'add', 'present', 'conflict', 'blocked'),
            'policy': {'priority_placements': sum(len(g['ids']) for g in policy['priority_groups']), 'relevance_overrides': len(policy['relevance_overrides']),
                       'category_placements': sum(len(v) for v in policy['groups'].values()), 'family_placements': sum(len(v) for v in policy['families'].values()),
                       'review_notes': len(policy['review_notes'])},
            'preaccess': {**tally(preaccess['screens'], 'add', 'present', 'skipped', 'blocked'), 'required_registration_ids': len(preaccess['census']['required_registration_ids']),
                          'deferred_registration_ids': len(preaccess['census']['deferred_registration_ids'])}}


def merge(source_root, target_root, apply=False, record=True, site_request=None):
    """Plan, and with `apply` write, the merge. `record=False` computes the plan without a run folder (the site's preview)."""
    if apply and not record:
        raise ValueError('apply_needs_a_recorded_run')
    source, source_id = workspace(source_root)
    target, target_id = workspace(target_root)
    if source == target:
        raise SystemExit('source_and_target_are_the_same_workspace: ' + str(source))
    at = now()
    run = target / 'runs/merge_study_facts' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    run_ref = str(run.relative_to(target)) if record else 'preview'
    if record:
        run.mkdir(parents=True)
    request = {'schema_version': 1, 'kind': 'datahunter_study_facts_merge', 'at': at, 'apply': apply,
               'source': {'study': source_id, 'workspace': str(source)}, 'target': {'study': target_id, 'workspace': str(target)},
               'code_sha256': digest(__file__)}
    if record:
        write(run / 'request.json', request)
        if site_request is not None:
            write(run / 'site-request.json', site_request)
    usage, selection, validation, policy = [read(target / 'configs' / name) for name in REGISTRIES]
    source_usage, source_selection, source_validation, source_policy = [read(source / 'configs' / name) for name in REGISTRIES]
    for document in (usage, policy, source_usage, source_policy):
        if document.get('schema_version') != 1:
            raise SystemExit('unsupported_registry_schema')
    datasets, papers = plan_catalogue(source_usage, usage, source_id)
    files = plan_notices(source, target)
    evidence = Evidence(files, source, target)
    target_hash = fingerprint(registered(target, target_id))
    licences = plan_licences(source_policy, policy, evidence, source_id, target_id, target_hash, run_ref, at)
    added = [d['entry'] for d in datasets if d['state'] == 'add']
    catalogue_ids = {d['id'] for d in usage['datasets']} | {d['id'] for d in added}
    placements = plan_policy(source_selection, selection, source_validation, validation, catalogue_ids)
    preaccess = plan_preaccess(source_selection, selection, placements, added, catalogue_ids, evidence, source_id, run_ref)
    report = {'schema_version': 1, 'kind': 'datahunter_study_facts_merge_report', 'at': at, 'applied': False, 'run': run_ref,
              'source': request['source'], 'target': request['target'], 'datasets': datasets, 'papers': papers, 'files': files,
              'licences': licences, 'policy': placements, 'preaccess': preaccess, 'not_merged': NOT_MERGED, 'data_permission_granted': False, 'model_calls': 0}
    if apply:
        for name in REGISTRIES:
            shutil.copyfile(target / 'configs' / name, run / ('before-' + name))
        shutil.copyfile(__file__, run / 'merge_study_facts.py')
        apply_plan(report, source, target, usage, selection, validation, policy)
        report['applied'] = True
    report['counts'] = counts_of(report)
    if record:
        write(run / 'report.json', report)
    if apply:
        c = report['counts']
        summary = (f"{c['datasets']['add']} catalogue entries added as pre-access, {c['papers']['add']} paper records, "
                   f"{c['licences']['add']} licence records re-bound with every operation unreviewed, {c['preaccess']['add']} terms screens (no rights granted), "
                   f"{c['files']['copied']} notice/evidence files copied, {c['policy']['priority_placements'] + c['policy']['category_placements']} policy placements; "
                   f"{c['datasets']['conflict'] + c['papers']['conflict'] + c['files']['conflict'] + c['licences']['conflict']} conflicts and "
                   f"{c['licences']['blocked'] + c['preaccess']['blocked']} blocked records left as they were. No file holdings and no AI-use permission transferred.")
        target_label = target.name if target.parent == STUDIES.resolve() else str(target)
        source_run = f'{target_label}/{run_ref}'
        report['log_events_added'] = len(append_log(target, target_id, [{'at': at, 'action': 'note', 'source': 'merge_study_facts.py', 'run': run_ref,
                                                                         'detail': f'Merged facts from study {source_id} ({source}): ' + summary}]))
        report['log_events_added'] += len(append_log(source, source_id, [{'at': at, 'action': 'note', 'source': 'merge_study_facts.py',
                                                                          **({'run': source_run} if len(source_run) <= 200 else {}),
                                                                          'detail': f'Facts copied into study {target_id} ({target}, its run {run_ref}): ' + summary + ' Nothing changed here.'}]))
        write(run / 'report.json', report)
    return report


def validate_request(request, expected_target=None):
    """Source and target workspaces of a merge request saved by the Research studies page. Nothing is written here; the
    plan is recomputed from the current registries when the request is run, so a stale request can only merge less."""
    if not isinstance(request, dict) or request.get('schema_version') != 1 or request.get('kind') != REQUEST_KIND:
        raise ValueError('unsupported_study_facts_merge_request')
    if request.get('reviewed') is not True:
        raise ValueError('reviewer_confirmation_required')
    for key in ('source', 'target'):
        if not isinstance(request.get(key), str) or not valid_id(request[key]):
            raise ValueError('invalid_merge_' + key)
    source, source_id = workspace(request['source'])
    target, target_id = workspace(request['target'])
    if source == target:
        raise ValueError('source_and_target_are_the_same_workspace')
    if expected_target is not None and workspace(expected_target)[0] != target:
        raise ValueError('merge_request_target_is_not_this_workspace: ' + target_id + ' (run with --target ' + target_id + ' or make it the active study)')
    return source, target


def plan_json(source, target):
    """The dry-run plan for the site's Merge control: counts, summary lines and conflicts. Nothing is written."""
    report = merge(source, target, apply=False, record=False)
    conflicts = [{'kind': kind, 'id': i.get('id') or i.get('path'), 'detail': ', '.join(i.get('differs_in', [])) or i.get('reason', '')}
                 for kind, items in (('dataset', report['datasets']), ('paper', report['papers']), ('file', report['files']), ('licence', report['licences']))
                 for i in items if i['state'] == 'conflict']
    return {'source': report['source']['study'], 'target': report['target']['study'], 'counts': report['counts'], 'summary': summary_lines(report),
            'conflicts': conflicts, 'data_permission_granted': False}


def summary_lines(report):
    c, applied = report['counts'], report['applied']
    ids = lambda items, state: ', '.join(i['id'] for i in items if i['state'] == state) or 'none'  # noqa: E731
    problems = lambda items: ''.join(f"\n    {i['id']}: {'; '.join(i['reasons'])}" for i in items if i['state'] == 'blocked')  # noqa: E731
    lines = [f"{'Merged' if applied else 'Dry run (registries and files untouched): would merge'} facts of study {report['source']['study']} into {report['target']['study']} "
             f"(run {report['run']})",
             f"- catalogue entries: {c['datasets']['add']} added as pre-access entries [{ids(report['datasets'], 'add')}]; {c['datasets']['present']} already present; "
             f"{c['datasets']['conflict']} conflicting, left untouched" + ''.join(f"\n    {d['id']}: differs in {', '.join(d['differs_in'])}" for d in report['datasets'] if d['state'] == 'conflict'),
             f"- paper records: {c['papers']['add']} added [{ids(report['papers'], 'add')}]; {c['papers']['present']} present; {c['papers']['conflict']} conflicting"
             + ''.join(f"\n    {p['id']}: differs in {', '.join(p['differs_in'])}" for p in report['papers'] if p['state'] == 'conflict'),
             f"- selection policy: {c['policy']['priority_placements']} priority placements, {c['policy']['relevance_overrides']} relevance overrides, "
             f"{c['policy']['category_placements']} category placements, {c['policy']['family_placements']} family placements, {c['policy']['review_notes']} review notes",
             f"- pre-access terms screens: {c['preaccess']['add']} copied with no rights granted [{ids(report['preaccess']['screens'], 'add')}]; {c['preaccess']['present']} present; "
             f"{c['preaccess']['skipped']} skipped; {c['preaccess']['blocked']} blocked; registration census +{c['preaccess']['required_registration_ids']} required, "
             f"+{c['preaccess']['deferred_registration_ids']} deferred" + problems(report['preaccess']['screens']),
             f"- notices and evidence files: {c['files']['copied'] if applied else c['files']['copy']} {'copied' if applied else 'to copy'}, {c['files']['present']} identical already, "
             f"{c['files']['conflict']} conflicting, {c['files']['missing'] + c['files']['held_file']} unavailable"
             + ''.join(f"\n    {f['path']}: {f['reason']}" for f in report['files'] if f['state'] in {'conflict', 'missing', 'held_file'}),
             f"- licence records: {c['licences']['add']} re-bound with every operation unreviewed [{ids(report['licences'], 'add')}]; {c['licences']['present']} present; "
             f"{c['licences']['conflict']} conflicting; {c['licences']['blocked']} blocked"
             + ''.join(f"\n    {r['id']}: differs in {', '.join(r['differs_in'])}" for r in report['licences'] if r['state'] == 'conflict') + problems(report['licences']),
             '- not merged: ' + '; '.join(f'{what} ({why})' for what, why in report['not_merged'].items()),
             'No file holdings and no AI-use permission cross over; the target study’s licence review and the source-bound preflight still decide.']
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument('--source', metavar='STUDY', help='Study id under studies/ or a workspace path whose facts are copied')
    what.add_argument('--request', type=Path, metavar='FILE', help='A datahunter_study_facts_merge_request saved by the Research studies page (names source and target)')
    parser.add_argument('--target', metavar='STUDY', help='Study id or workspace path that receives them (default: the active study)')
    parser.add_argument('--apply', action='store_true', help='Write the merge; without it the run only reports what would cross over')
    args = parser.parse_args(argv)
    if args.target is None:
        require_workspace(ROOT)
    if args.request:
        request = read(args.request)
        source, target = validate_request(request, expected_target=args.target if args.target is not None else ROOT)
        report = merge(source, target, apply=args.apply, site_request=request)
    else:
        report = merge(args.source, args.target if args.target is not None else ROOT, apply=args.apply)
    print('\n'.join(summary_lines(report)))
    return report


if __name__ == '__main__':
    main()
