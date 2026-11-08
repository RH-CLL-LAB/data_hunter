#!/usr/bin/env python3
"""Register a user-supplied research brief; this grants no data permissions.

A new study gets its own workspace folder, studies/<id>/, with empty registries,
and becomes the active study. Registering an existing study again updates its
shortlist, annotations and reports in that folder. Removing a study retires it
(logged in its action log, no longer active); its folder and data are kept.

    python Scripts/register_research_study.py --request datahunter-research-study.json --apply
    python Scripts/register_research_study.py --remove old-study --apply
"""
import argparse
from datetime import datetime, timezone
import json
import hashlib
import shutil
from pathlib import Path
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
sys.path.insert(0, str(CODE_ROOT / 'Scripts'))
from datahunter.research_studies import normalize, fingerprint, append_log, validate_log_event, EXECUTED_ACTIONS, default_study, reports_dir  # noqa: E402
from datahunter.workspace import (STUDIES, active_study_id, clear_active, create_study_workspace, is_study_workspace,  # noqa: E402
                                  set_active, study_path, workspace_root, workspace_study_id)

ROOT = workspace_root(default=CODE_ROOT)


def study_reports(reports, study_id, root):
    """Validate tracked study reports: Markdown in the study's reports folder with a matching hash."""
    if not isinstance(reports, list) or len(reports) > 50:
        raise ValueError('invalid_study_reports')
    base = reports_dir(root, study_id).resolve()
    cleaned = []
    for report in reports:
        if not isinstance(report, dict) or not all(isinstance(report.get(k), str) and report[k] for k in ['title', 'date', 'path', 'sha256']):
            raise ValueError('invalid_study_report')
        target = (Path(root) / report['path']).resolve()
        if target.suffix != '.md' or not target.is_relative_to(base) or not target.is_file():
            raise ValueError('study_report_must_be_tracked_markdown_in_study_folder')
        if hashlib.sha256(target.read_bytes()).hexdigest() != report['sha256']:
            raise ValueError('study_report_changed')
        cleaned.append({k: report[k] for k in ['title', 'date', 'path', 'sha256', 'run'] if isinstance(report.get(k), str)})
    return cleaned


def resolve_workspace(study_id, root):
    """Where this study lives. Explicit roots are used as given (legacy multi-study layout);
    otherwise the study's own folder, created on apply when it does not exist yet."""
    if root is not None:
        return Path(root), False
    folder = study_path(study_id)
    return folder, not is_study_workspace(folder)


def register(path, apply=False, root=None):
    value = json.loads(Path(path).read_text())
    if value.get('kind') not in ('datahunter_research_study', 'medscrape_research_study') or value.get('schema_version') != 1:
        raise ValueError('invalid_research_study_request')
    profile = normalize(value['study'])
    root, is_new = resolve_workspace(profile['id'], root)
    if is_new and apply:
        create_study_workspace(profile['id'])
        is_new = False
    pending = is_new  # dry run of a study that has no folder yet
    base = root if not pending else STUDIES / '.validation' / profile['id']
    for key in ['dataset_annotations', 'study_annotations']:
        annotations = value.get(key, {})
        if not isinstance(annotations, dict):raise ValueError('invalid_study_annotations')
        cleaned = {}
        for identifier, item in annotations.items():
            if not isinstance(identifier, str) or not isinstance(item, dict):raise ValueError('invalid_study_annotation')
            if item.get('relevance', 'unassessed') not in {'high','moderate','low','unassessed'} or item.get('role','unassessed') not in {'development','benchmark','reference','exclude','unassessed'}:raise ValueError('invalid_study_annotation_value')
            tags = item.get('tags', [])
            if not isinstance(tags,list) or len(tags)>20 or any(not isinstance(t,str) or len(t)>60 for t in tags):raise ValueError('invalid_study_annotation_tags')
            note = item.get('note','')
            if not isinstance(note,str) or len(note)>4000:raise ValueError('invalid_study_annotation_note')
            cleaned[identifier]={'relevance':item.get('relevance','unassessed'),'role':item.get('role','unassessed'),'tags':tags,'note':note}
        profile[key]=cleaned
    profile['reports']=study_reports(value.get('reports',[]),profile['id'],root)
    ids=value.get('dataset_ids',[])
    if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids):raise ValueError('invalid_study_shortlist')
    profile['dataset_ids']=list(dict.fromkeys(ids))
    target = root / 'configs/research_studies.json'
    registry = json.loads(target.read_text()) if target.exists() else {'schema_version': 1, 'studies': [], **({'default_study': profile['id']} if workspace_study_id(root) == profile['id'] else {})}
    old = next((s for s in registry['studies'] if s['id'] == profile['id']), None)
    if old and profile['id'] == default_study(root) and fingerprint(old) != fingerprint(profile):
        # Permissions are bound to the brief; a changed question or use is a new study.
        raise ValueError('brief_changed_create_a_new_study_instead_of_changing_this_one')
    if old and fingerprint(old) != fingerprint(profile) and value.get('previous_study_sha256') != fingerprint(old):
        raise ValueError('existing_study_changed_export_current_revision_before_update')
    # Reports are cumulative study records: a later registration (e.g. a browser export) keeps them.
    if old and fingerprint(old) == fingerprint(profile):
        known={r['path'] for r in profile['reports']}
        profile['reports']=[r for r in old.get('reports',[]) if r['path'] not in known]+profile['reports']
    if not profile['reports']:del profile['reports']
    browser_log = value.get('log', [])
    if not isinstance(browser_log, list) or len(browser_log) > 1000:raise ValueError('invalid_study_log')
    browser_log = [validate_log_event({**e, 'source': 'browser'}) for e in browser_log]
    if any(e['action'] in EXECUTED_ACTIONS for e in browser_log):
        raise ValueError('browser_log_cannot_record_executed_actions')
    run = base / 'runs/research_studies' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    run.mkdir(parents=True)
    (run / 'request.json').write_text(json.dumps(value, indent=2) + '\n')
    shutil.copyfile(__file__,run/'registration-code.py')
    (run / 'registry-before.json').write_text(json.dumps(registry, indent=2) + '\n')
    if apply:
        registry['studies'] = [s for s in registry['studies'] if s['id'] != profile['id']] + [profile]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(registry, indent=2) + '\n')
    result = {'study_id': profile['id'], 'study_sha256': fingerprint(profile), 'applied': apply,
              'status': 'registered' if apply else 'validated_pending_registration', 'data_permission_granted': False,
              'workspace': str(root), 'request_sha256': hashlib.sha256((run/'request.json').read_bytes()).hexdigest()}
    if apply:
        added = sorted({r['path'] for r in profile.get('reports', [])} - {r['path'] for r in (old or {}).get('reports', [])})
        summary = (f"{len(profile['dataset_ids'])} shortlisted, {len(profile['dataset_annotations'])} dataset and "
                   f"{len(profile['study_annotations'])} paper annotations; no data permission granted")
        events = browser_log + [{'at': datetime.now(timezone.utc).isoformat(), 'action': 'study_updated' if old else 'study_registered',
                                 'detail': summary, 'source': 'register_research_study.py', 'run': str(run.relative_to(root))}]
        events += [{'at': datetime.now(timezone.utc).isoformat(), 'action': 'report_added', 'detail': p, 'source': 'register_research_study.py'} for p in added]
        result['log_events_added'] = len(append_log(root, profile['id'], events))
        if workspace_study_id(root) == profile['id']:
            set_active(profile['id'])
            result['active_study'] = profile['id']
    (run / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
    (run / 'registry-after.json').write_text(json.dumps(registry, indent=2) + '\n')
    print(json.dumps(result)); return result


def remove(study_id, apply=False, root=None):
    """Retire a study. In its own workspace this logs the removal and deactivates it; the folder,
    registries, data and log are kept. In a legacy multi-study root the registry entry is removed."""
    if root is None:
        folder = study_path(study_id)
        if not is_study_workspace(folder):
            raise ValueError('unknown_research_study')
        return retire_workspace(study_id, folder, apply)
    root = Path(root)
    if study_id == default_study(root):
        raise ValueError('the_default_study_cannot_be_removed')
    target = root / 'configs/research_studies.json'
    registry = json.loads(target.read_text()) if target.exists() else {'schema_version': 1, 'studies': []}
    old = next((s for s in registry['studies'] if s['id'] == study_id), None)
    if not old:
        raise ValueError('unknown_research_study')
    run = root / 'runs/research_studies' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    run.mkdir(parents=True)
    (run / 'request.json').write_text(json.dumps({'kind': 'datahunter_research_study_removal', 'study_id': study_id}, indent=2) + '\n')
    (run / 'registry-before.json').write_text(json.dumps(registry, indent=2) + '\n')
    result = {'study_id': study_id, 'study_sha256': fingerprint(old), 'applied': apply, 'run': str(run.relative_to(root)),
              'kept': f'docs/research-studies/{study_id}/ (legacy layout: reports and action log)', 'data_permissions_changed': False}
    if apply:
        registry['studies'] = [s for s in registry['studies'] if s['id'] != study_id]
        target.write_text(json.dumps(registry, indent=2) + '\n')
        detail = (f"Removed from configs/research_studies.json ({len(old.get('dataset_ids', []))} shortlisted, "
                  f"{len(old.get('dataset_annotations', {}))} dataset annotations). Reports and this log are kept; no data permission changed.")
        result['log_events_added'] = len(append_log(root, study_id, [{'at': datetime.now(timezone.utc).isoformat(), 'action': 'study_removed',
                                                                        'detail': detail, 'source': 'register_research_study.py', 'run': result['run']}]))
    (run / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
    (run / 'registry-after.json').write_text(json.dumps(registry, indent=2) + '\n')
    print(json.dumps(result)); return result


def retire_workspace(study_id, folder, apply):
    marker = json.loads((folder / 'study.json').read_text())
    registry = json.loads((folder / 'configs/research_studies.json').read_text())
    old = next((s for s in registry['studies'] if s['id'] == study_id), None)
    run = folder / 'runs/research_studies' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    run.mkdir(parents=True)
    (run / 'request.json').write_text(json.dumps({'kind': 'datahunter_research_study_removal', 'study_id': study_id}, indent=2) + '\n')
    result = {'study_id': study_id, 'study_sha256': fingerprint(old) if old else None, 'applied': apply, 'run': str(run.relative_to(folder)),
              'kept': f'{folder.relative_to(CODE_ROOT)}/ (registries, data, runs, reports and action log)', 'data_permissions_changed': False,
              'was_active': active_study_id() == study_id}
    if apply:
        marker['retired_at'] = datetime.now(timezone.utc).isoformat()
        (folder / 'study.json').write_text(json.dumps(marker, indent=2) + '\n')
        clear_active(study_id)
        detail = (f"Study retired: no longer the active workspace. Its folder {result['kept']} is kept with "
                  f"{len((old or {}).get('dataset_ids', []))} shortlisted datasets; delete the folder yourself to discard its data. No data permission changed.")
        result['log_events_added'] = len(append_log(folder, study_id, [{'at': datetime.now(timezone.utc).isoformat(), 'action': 'study_removed',
                                                                          'detail': detail, 'source': 'register_research_study.py', 'run': result['run']}]))
    (run / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result)); return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--request', type=Path, help='Exported study JSON to validate and register')
    group.add_argument('--remove', metavar='STUDY_ID', help='Retire a registered study (its folder, reports and log are kept)')
    parser.add_argument('--apply', action='store_true', help='Write the change; without it the run is a dry validation')
    args = parser.parse_args()
    remove(args.remove, args.apply) if args.remove else register(args.request, args.apply)
