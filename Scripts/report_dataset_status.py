#!/usr/bin/env python3
"""Write the plain-language dataset status: what was obtained automatically, what is partial, what needs which action.

Reads the current catalogue receipt, the dataset registry, the exact-artifact reviews, the acquisition lock,
the licence registers, the download-run reports and the quarantine listing, and writes docs/DATASET_STATUS.md:

  1. an at-a-glance table, one row per dataset, with exactly one state
       AUTOMATIC      the complete release is held and verified; nothing to do
       PARTIAL        the permitted part is held; the rest needs the listed action
       ACTION NEEDED  nothing is held until the listed step is done
       NO ROUTE       no public access route exists for this project
     plus the held/release counts, the one action for the user and the access route;
  2. the datasets grouped by what unblocks them: a technical-exception tick, a licence decision,
     an access agreement or registration, per-source terms, no route;
  3. quarantine contents (pending deliveries and superseded duplicates) and the licence records that still
     need a human confirmation.

`document_dataset_selection.py` regenerates the document with the catalogue; `--report` also saves a dated,
tracked copy under reports/ and registers it with the study. Offline, deterministic, no model calls; the
document grants nothing.
"""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_quarantine  # noqa: E402

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root, require_workspace, workspace_study_id  # noqa: E402
from datahunter.research_studies import default_study, fingerprint as study_fingerprint  # noqa: E402

ROOT = workspace_root(default=CODE_ROOT)
STATES = ('AUTOMATIC', 'PARTIAL', 'ACTION NEEDED', 'NO ROUTE')
CAUSES = OrderedDict([('tick', 'Needs one tick from you: a technical exception'), ('licence', 'Needs your licence decision'),
                      ('agreement', 'Needs an access agreement or registration only you can complete'), ('terms', 'Needs per-source terms before any transfer'),
                      ('route', 'No route for this project'), ('open', 'Open release not yet acquired')])
ROUTE_ACTIONS = {
    'registration_credentialed_access': ('agreement', 'Complete the provider credentialing (identity, required training, signed data use agreement){at}; then the agent pins the release manifest and size.'),
    'registration_credentialed_contributor_review': ('agreement', 'Complete the provider credentialing{at} and submit the study description for the contributors’ review; transfer only after their approval.'),
    'registration_data_use_agreement': ('agreement', 'Create the provider account, read and accept the data use agreement in your browser{at} and keep its text; then the agent pins the release.'),
    'registration_institutional_end_user_license': ('agreement', 'Apply{at} with an institutional address and the required certificates, accept the End User License and keep it; files follow after approval.'),
    'registration_click_through': ('agreement', 'Register{at}, accept the click-through licence and keep it; then the agent plans the download.'),
    'public_pending_file_level_review': ('licence', 'Decide the data terms in the dataset’s licence review (the retained notices are linked there); a software licence alone is not a data grant.'),
    'public_mixed_sources': ('terms', 'Review each component source’s terms; the agent fetches the permitted components and lists the rest.'),
    'no_public_access_route': ('route', 'No public access route exists for this project; the entry is context only.'),
}
RISK_ACTIONS = {'missing_provider_checksum': 'tick “missing provider checksum” in the Download dialog and re-send the instructions (the first transfer is quarantined once, then reviewed and released)',
                'unknown_advertised_size': 'tick “unknown advertised size” in the Download dialog and re-send the instructions'}


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def latest_run_statuses(root):
    """Per dataset: the artifact statuses of its most recent download-agent run (technical exceptions, licence blocks)."""
    runs = sorted((root / 'runs/downloads').glob('*/report.json')) if (root / 'runs/downloads').is_dir() else []
    seen = {}
    for path in runs:
        report = read(path)
        for result in report.get('dataset_results', []):
            statuses = Counter(a['status'] for a in result.get('artifacts', []))
            risks = Counter(r for a in result.get('artifacts', []) for r in a.get('unaccepted', []))
            seen[result['dataset']] = {'run': path.parent.name, 'status': result['status'], 'artifact_statuses': dict(statuses), 'unaccepted_risks': dict(risks)}
    return seen


def collect(root):
    root = Path(root).resolve()
    receipt = read(root / 'data/interim/dataset_selection/latest.json')
    catalogue = read(root / receipt['selection'])
    usage = {d['id']: d for d in read(root / 'configs/dataset_study_usage.json')['datasets']}
    reviews = read(root / 'configs/dataset_download_reviews.json') if (root / 'configs/dataset_download_reviews.json').exists() else {}
    lock_path = root / 'configs/research_resources.lock.json'
    locked = {f['id']: f for f in read(lock_path)['files']} if lock_path.exists() else {}
    licences = read(root / 'configs/data_licenses.json')['resources'] if (root / 'configs/data_licenses.json').exists() else []
    human_path = root / 'configs/dataset_license_reviews.json'
    human = [r for r in read(human_path).get('reviews', []) if r.get('applied')] if human_path.exists() else []
    studies = read(root / 'configs/research_studies.json')
    return {'root': root, 'receipt': receipt, 'datasets': catalogue['datasets'], 'usage': usage, 'reviews': reviews, 'locked': locked, 'licences': licences, 'human': human,
            'studies': studies, 'runs': latest_run_statuses(root), 'quarantine': review_quarantine.inventory(root, write=False)}


def held_counts(d, data):
    """Held files versus the release the registry knows: aliases in the exact-artifact review, or the catalogue's held count."""
    review = next((r for r in data['reviews'].get('reviews', []) if r['id'] == d['id']), None)
    held = d.get('associated_download_files') or 0
    if review and review.get('full_release', {}).get('files'):
        return held, review['full_release']['files'], review
    return held, (held if d['completeness'] == 'complete_public_release' else None), review


def licence_state(d, data):
    """Allowed records for the dataset, whether a human review backs them, and records that stay unreviewed."""
    records = [r for r in data['licences'] if d['id'] in r.get('catalog_ids', [])]
    allowed = [r for r in records if r['operations'].get('local_inference') == 'allowed' and r['operations'].get('local_embedding') == 'allowed' and r.get('scope') != 'software_only']
    reviewed = {r['dataset_id'] for r in data['human'] if str(r.get('status', '')).startswith('approved')}
    software = [r for r in records if r.get('scope') == 'software_only']
    return {'records': [r['id'] for r in records], 'allowed': [r['id'] for r in allowed], 'confirm': [r['id'] for r in allowed if d['id'] not in reviewed],
            'unreviewed': [r['id'] for r in records if r not in allowed and r not in software], 'software_only': [r['id'] for r in software]}


def classify(d, data):
    held, release, review = held_counts(d, data)
    route = d.get('access_route', '')
    run = data['runs'].get(d['id'], {})
    risks = [r for r in run.get('unaccepted_risks', {})]
    missing = ' '.join(d.get('missing_components', []))
    if route == 'no_public_access_route' or d.get('category') == 'excluded':
        state, cause = 'NO ROUTE', 'route'
    elif held and d['completeness'] == 'complete_public_release':
        state, cause = 'AUTOMATIC', None
    elif held:
        state, cause = 'PARTIAL', None
    else:
        state, cause = 'ACTION NEEDED', None
    actions = []
    if state in {'PARTIAL', 'ACTION NEEDED'}:
        if risks or 'no checksum' in missing.lower() or 'acknowledgement' in missing.lower():
            cause = cause or 'tick'
            actions.append(RISK_ACTIONS.get(risks[0], RISK_ACTIONS['missing_provider_checksum']) if risks else RISK_ACTIONS['missing_provider_checksum'])
        if route in ROUTE_ACTIONS:
            kind, text = ROUTE_ACTIONS[route]
            cause = cause or kind
            at = f' at {d["registration_url"]}' if d.get('registration_url') else ''
            actions.append(text.format(at=at))
        elif route == 'public_no_registration' and not actions and state == 'ACTION NEEDED':
            cause = cause or 'open'
            actions.append(d.get('next_action') or 'Open release; ask the agent to pin its manifest and acquire it.')
        if state == 'PARTIAL':
            actions.append('Remaining part: ' + (' '.join(d['missing_components']) if d.get('missing_components') else 'see the dataset page'))
    lic = licence_state(d, data)
    if lic['confirm'] and state in {'AUTOMATIC', 'PARTIAL'}:
        actions.append('confirm the licence record ' + ', '.join(lic['confirm']) + ' in the dataset’s licence review')
    if state in {'AUTOMATIC', 'PARTIAL'} and not lic['records']:
        actions.append('held rights-unresolved: record the source terms in the dataset’s licence review before any AI use')
    if state == 'AUTOMATIC' and not actions:
        actions.append('nothing' + (' (software release; never an AI data input)' if lic['software_only'] and not lic['allowed'] else ''))
    if state == 'NO ROUTE':
        actions = [ROUTE_ACTIONS['no_public_access_route'][1]]
    return {'id': d['id'], 'name': d['name'], 'state': state, 'cause': cause, 'held': held, 'release': release, 'route': route, 'actions': actions, 'licence': lic,
            'completeness': d['completeness'], 'next_action': d.get('next_action', ''), 'missing': d.get('missing_components', []), 'run': run,
            'bytes': d.get('associated_download_bytes'), 'release_name': (review or {}).get('full_release', {}).get('manifest')}


def fmt_held(row):
    if row['state'] == 'NO ROUTE':
        return '0'
    if row['release'] is not None:
        return f"{row['held']:,} / {row['release']:,} files"
    return f"{row['held']:,} files" if row['held'] else '0'


def render(data):
    rows = [classify(d, data) for d in data['datasets']]
    counts = Counter(r['state'] for r in rows)
    study_id = workspace_study_id(data['root']) or default_study(data['root'])
    study = next((s for s in data['studies']['studies'] if s['id'] == study_id), None)
    quarantine = data['quarantine']
    out = ['# Dataset status and next actions', '',
           f"Study `{study_id}`" + (f": {study['name']}" if study else '') + f". Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from the catalogue receipt "
           f"`{data['receipt']['selection_sha256'][:16]}…`, the acquisition lock ({len(data['locked'])} verified files), the exact-artifact reviews, the licence registers, the download-run "
           'reports and the quarantine listing. Every state is derived from those records; nothing here grants data or AI-use permission, and downloading never does.', '',
           '## At a glance', '',
           '**AUTOMATIC** means the agent obtained the complete release and verified it; nothing to do. **PARTIAL** means the permitted part is held and the rest needs the listed action. '
           '**ACTION NEEDED** means nothing is held until you do the listed step. **NO ROUTE** means no public access route exists for this project.', '',
           '| Dataset | State | Held | What you need to do | Access route |', '|---|---|---|---|---|']
    for r in sorted(rows, key=lambda r: (STATES.index(r['state']), r['id'])):
        out.append(f"| {r['id']} | **{r['state']}** | {fmt_held(r)} | {'; '.join(r['actions']).replace('|', '\\|')} | {r['route'].replace('_', ' ')} |")
    out += ['', 'Counts: ' + ', '.join(f"{counts.get(s, 0)} {s.lower()}" for s in STATES) + '.', '']
    automatic = [r for r in rows if r['state'] == 'AUTOMATIC']
    if automatic:
        out += ['## Obtained automatically', '', '| Dataset | Files | Bytes held | Where | Licence basis |', '|---|---|---|---|---|']
        for r in automatic:
            d = next(x for x in data['datasets'] if x['id'] == r['id'])
            where = ', '.join(d.get('source_locators', [])) or '—'
            basis = ', '.join(r['licence']['allowed']) or (', '.join(r['licence']['software_only']) + ' (software only)' if r['licence']['software_only'] else 'no allowed record')
            out.append(f"| {r['id']} | {fmt_held(r)} | {r['bytes']:,} | `{where}` | {basis} |" if r['bytes'] is not None else f"| {r['id']} | {fmt_held(r)} | — | `{where}` | {basis} |")
        out.append('')
    partial = [r for r in rows if r['state'] == 'PARTIAL']
    if partial:
        out += ['## Partially obtained', '']
        for r in partial:
            out += [f"### {r['id']} — {r['name']}", '', f"Held: {fmt_held(r)}" + (f" ({r['bytes']:,} bytes)" if r['bytes'] else '') + f". Completeness: `{r['completeness']}`.", '']
            out += [f'- Missing: {m}' for m in r['missing']] or ['- Missing part not itemised in the registry.']
            out += [f"- Next: {r['next_action']}", '']
    for cause, title in CAUSES.items():
        group = [r for r in rows if r['state'] in {'ACTION NEEDED', 'NO ROUTE'} and r['cause'] == cause]
        if not group:
            continue
        out += [f'## {title}', '', '| Dataset | Access route | What you need to do | Registry note |', '|---|---|---|---|']
        for r in group:
            out.append(f"| {r['id']} | {r['route'].replace('_', ' ')} | {'; '.join(r['actions']).replace('|', '\\|')} | {(r['next_action'] or '').replace('|', '\\|')} |")
        out.append('')
    out += ['## Quarantine', '', "**What quarantine is.** `data/quarantine/download_agent/<run>/` is a staging folder, separate from the verified holdings under `data/acquisitions/`. A file lands there in two cases: it was transferred under a technical exception you accepted (the provider published no checksum, or no exact size), so its bytes cannot be proven to match the provider's artifact; or the download agent transferred it in minimal scope, which always stages new files until a review registers them, even when the provider identity verified. Nothing in quarantine is held, locked or usable: it is not in the acquisition lock, carries no licence-asset identity and the preflight never sees it. A human release (below) moves a staged file into the holdings after re-verification; a discard removes a staged copy whose bytes are already held. This is different from held files whose AI use is blocked by a licence record: those are in the holdings, and only the registry entry stops their use.", '']
    if not quarantine['items']:
        out += ['The quarantine folder is empty: every transferred file carried a provider identity and went straight into the locked holdings.', '']
    else:
        out += [f"{len(quarantine['items'])} staged files ({quarantine['bytes']:,} bytes) under `data/quarantine/download_agent/`: " + ', '.join(f'{n} {s.replace("_", " ")}' for s, n in sorted(quarantine['summary'].items())) + '.',
                'Pending deliveries are released, and superseded duplicates discarded, from the Quarantine page (tick the files, save the request, hand it to the agent with `Scripts/review_quarantine.py`). '
                'A release re-verifies the bytes, runs the acquisition format checks, can compare a second download, then locks the file and binds its licence assets; it grants no AI use.', '',
                '| Run | Dataset(s) | File | Bytes | State | Why it is in quarantine |', '|---|---|---|---|---|---|']
        for i in quarantine['items'][:200]:
            out.append(f"| {i['run']} | {', '.join(i.get('dataset_ids') or []) or '—'} | `{i['job_path']}` | {i['bytes'] if i.get('bytes') is not None else '—'} | {i['state'].replace('_', ' ')} | {(i.get('reason') or '').replace('|', '\\|')} |")
        if len(quarantine['items']) > 200:
            out.append(f"| … | | {len(quarantine['items']) - 200} more files in the listing | | | |")
        out.append('')
    held_blocked = [r for r in rows if r['held'] and (r['licence']['confirm'] or r['licence']['unreviewed'] or not r['licence']['records'] or (r['licence']['software_only'] and not r['licence']['allowed']))]
    out += ['## Held, but not yet cleared for AI use', '',
            'These files are downloaded, verified and locked under `data/acquisitions/`. What stops their use is the registry entry, enforced by the source-bound preflight before any inference or embeddings, not their location on disk. '
            'The dataset page (linked) carries the licence review that records your decision.', '']
    if held_blocked:
        out += ['| Dataset | Held | Why AI use is still blocked | What you need to do |', '|---|---|---|---|']
        for r in held_blocked:
            lic = r['licence']
            why, todo = [], []
            if lic['confirm']:
                why.append('licence record(s) written by the agent from public notices, not yet confirmed by a human: ' + ', '.join(f'`{x}`' for x in lic['confirm']))
                todo.append('confirm or revise the record in the dataset’s licence review (verdict approve, the operations you intend, the retained notice as evidence)')
            if lic['unreviewed']:
                why.append('unreviewed record(s): ' + ', '.join(f'`{x}`' for x in lic['unreviewed']))
                todo.append('record the applicable terms or agreement in the licence review before any AI use')
            if not lic['records']:
                why.append('no licence record at all: acquired as open-access data under the explicit unresolved-rights option, or registered before any licence screen')
                todo.append('read the source terms, retain the notice and record the decision in the dataset’s licence review')
            if lic['software_only'] and not lic['allowed']:
                why.append('software-only licence (' + ', '.join(f'`{x}`' for x in lic['software_only']) + '): code and definitions, never an AI data input')
                todo.append('nothing; use the code under its licence, keep it out of model inputs')
            out.append(f"| [{r['id']}](DATASET_SELECTION.md#{r['id']}) | {fmt_held(r)} | {'; '.join(why).replace('|', '\\|')} | {'; '.join(todo)} |")
        out.append('')
    else:
        out += ['None: every held dataset has an allowed, human-reviewed licence record.', '']
    caveats = [(d['id'], d['data_caveats']) for d in data['datasets'] if d.get('data_caveats') and d.get('associated_download_files')]
    if caveats:
        out += ['## What to check before using the held files', '', 'Recorded at acquisition from the registries: what is uncertain about the held files, where to look and what to expect. None of it is a defect finding; it is what the provider did or did not state.', '']
        for dataset_id, items in caveats:
            out += [f'### {dataset_id}', ''] + [f'- {c}' for c in items] + ['']
    confirm = [(r['id'], rec) for r in rows for rec in r['licence']['confirm']]
    out += ['## Licence records that need your confirmation', '']
    if confirm:
        out += ['These records allow lenient local inference and embeddings for the study and were written from public notices by the agent, not by a human review. '
                'Confirm or revise each through the dataset’s licence review (`Scripts/review_dataset_license.py`); strict mode would block all of them until an explicit AI approval exists.', '',
                '| Dataset | Record |', '|---|---|'] + [f'| {d} | `{rec}` |' for d, rec in confirm] + ['']
    else:
        out += ['None: every allowed licence record is backed by an applied human review.', '']
    unreviewed = [(r['id'], rec) for r in rows for rec in r['licence']['unreviewed']]
    if unreviewed:
        out += ['## Licence records still unreviewed', '', 'Blocked for AI use until the access steps above are done and the agreement or decision is recorded.', '',
                '| Dataset | Record |', '|---|---|'] + [f'| {d} | `{rec}` |' for d, rec in unreviewed] + ['']
    out += ['## Before any AI use', '',
            f'Run the source-bound preflight with `DATA_HUNTER_STUDY_ID={study_id}` and the saved licence mode. Holding a file is never permission to use it; '
            'remote inference, training and redistribution need their own decisions. After you complete an access step, re-export the download instructions for that dataset and hand them to the agent.', '']
    return '\n'.join(out), rows, counts


def write_document(root=ROOT):
    root = Path(root).resolve()
    data = collect(root)
    text, rows, counts = render(data)
    doc = root / 'docs/DATASET_STATUS.md'
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text)
    return doc, rows, counts


def save_report(root, doc, title='Dataset status and next actions', stem='dataset-status', runs_dir='status_reports'):
    """Save a dated tracked copy under reports/ and register it with the workspace's study. Other generated documents
    (the data-structure report) reuse it with their own title and file stem."""
    root = Path(root).resolve()
    study_id = workspace_study_id(root)
    if not study_id:
        raise ValueError('report_needs_a_study_workspace')
    today = date.today().isoformat()
    target = root / 'reports' / f'{stem}-{today}.md'
    target.parent.mkdir(exist_ok=True)
    target.write_text(re.sub(r'\]\(((?:DATASET_[A-Z_]+|DATA_LICENSES_AND_AI_USE)\.md(?:#[^)]*)?)\)', r'](../docs/\1)', doc.read_text()))  # the document is written for docs/; from reports/ its sibling links need the docs/ prefix
    registry = read(root / 'configs/research_studies.json')
    study = next(s for s in registry['studies'] if s['id'] == study_id)
    request = {'schema_version': 1, 'kind': 'datahunter_research_study', 'study': {k: study[k] for k in ['id', 'name', 'question', 'intended_use', 'population', 'targets', 'inclusion', 'exclusion']},
               'dataset_ids': study.get('dataset_ids', []), 'dataset_annotations': study.get('dataset_annotations', {}), 'study_annotations': study.get('study_annotations', {}),
               'reports': [{'title': f'{title} ({today})', 'date': today, 'path': str(target.relative_to(root)), 'sha256': digest(target)}], 'log': []}
    request_path = root / 'runs' / runs_dir / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') / 'study_update.json'
    request_path.parent.mkdir(parents=True)
    request_path.write_text(json.dumps(request, indent=2, ensure_ascii=False) + '\n')
    sys.path.insert(0, str(CODE_ROOT / 'Scripts'))
    from register_research_study import register
    from datahunter.workspace import active_study_id, set_active
    previous = active_study_id()
    register(request_path, apply=True, root=root)
    if previous and previous != study_id:
        try:
            set_active(previous)  # registering a report is not a request to switch studies
        except ValueError:
            pass
    return target


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--report', action='store_true', help='Also save a dated tracked copy under reports/ and register it with the study')
    args = parser.parse_args(argv)
    doc, rows, counts = write_document(root)
    result = {'document': str(doc), 'datasets': len(rows), 'states': {s: counts.get(s, 0) for s in STATES}}
    if args.report:
        result['report'] = str(save_report(root, doc))
    print(json.dumps(result))
    return result


if __name__ == '__main__':
    require_workspace(ROOT)
    main()
