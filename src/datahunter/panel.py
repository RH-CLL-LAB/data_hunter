"""Activity panel: what is running, what each study has done, what is missing. Read-only; it grants nothing.

`panel_model()` is served by Scripts/serve.py at /api/panel and rendered by the site's activity page.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

try:
    from .workspace import STUDIES, is_study_workspace
except ImportError:  # loaded as a plain file
    from workspace import STUDIES, is_study_workspace

SCRIPT = re.compile(r'Scripts/([a-z_]+\.py)(.*)')
INTERPRETER = re.compile(r'^(?:\S*/)?python[\d.]*$')
STUDY_IN_ARGS = re.compile(r'studies/([^/\s]+)/')
COUNTS = re.compile(r'Counts: (\d+) automatic, (\d+) partial, (\d+) action needed, (\d+) no route')
MILESTONES = ('Registered', 'Discovery run', 'Catalogue built', 'Status report', 'Downloads started', 'All registered files held')


def read(path):
    return json.loads(Path(path).read_text())


def running_processes(ps_output=None):
    """Data Hunter scripts running now, from `ps`: pid, elapsed time, CPU, script, arguments and the study named in them."""
    if ps_output is None:
        ps_output = subprocess.run(['ps', '-axo', 'pid,etime,%cpu,command'], capture_output=True, text=True).stdout
    rows = []
    for line in ps_output.splitlines()[1:]:
        if 'Scripts/' not in line or '.py' not in line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, etime, cpu, command = parts
        m = SCRIPT.search(command)
        if not m:
            continue
        head = command[:m.start()].split()
        if not head or not INTERPRETER.match(head[0]) or any(not t.startswith('-') for t in head[1:]):
            continue  # a wrapper around the script (caffeinate, nohup, the app's launcher): its child is listed on its own
        study = STUDY_IN_ARGS.search(command)
        rows.append({'pid': int(pid), 'elapsed': etime, 'cpu': cpu, 'script': m.group(1), 'args': m.group(2).strip()[:160],
                     'study': study.group(1) if study else None, 'kind': 'site server' if m.group(1) == 'serve.py' else 'job'})
    return rows


def resolver_progress(study_dir):
    """Phase and completion of a full-release resolver run still in flight for this study (its newest journal without a report)."""
    folder = Path(study_dir) / 'runs/full_release'
    if not folder.is_dir():
        return None
    for journal in sorted(folder.glob('*/operations.jsonl'))[-3:][::-1]:
        if (journal.parent / 'report.json').exists():
            continue
        rows = [json.loads(l) for l in journal.read_text().splitlines() if l.strip()]
        if not rows:
            continue
        ids = rows[0]['details'].get('only') or rows[0]['details'].get('dataset_ids') or []
        resolved = [r for r in rows if r['kind'] == 'dataset_resolved']
        planned = sum(r['details'].get('new_jobs') or 0 for r in resolved)
        acquired = sum(1 for r in rows if r['kind'] == 'acquired')
        failed = sum(1 for r in rows if r['kind'] == 'transfer_failed')
        if len(resolved) < len(ids):
            return {'run': journal.parent.name, 'phase': f'resolving {len(resolved)} of {len(ids)} releases', 'percent': 100.0 * len(resolved) / max(1, len(ids)), 'last_event': rows[-1]['at']}
        return {'run': journal.parent.name, 'phase': f'transferring {acquired:,} of {planned:,} files' + (f', {failed} failed' if failed else ''),
                'percent': 100.0 * acquired / max(1, planned), 'last_event': rows[-1]['at']}
    return None


def newest_change(folder, limit=4000):
    newest = 0.0
    if Path(folder).is_dir():
        for p in list(Path(folder).rglob('*'))[:limit]:
            try:
                if p.is_file():
                    newest = max(newest, p.stat().st_mtime)
            except OSError:
                pass
    return datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec='seconds') if newest else None


def study_summary(study_dir, running_studies=()):
    """Milestones, holdings, status counts and what is missing for one study workspace."""
    study = Path(study_dir); sid = study.name
    actions = [json.loads(l) for l in (study / 'actions.jsonl').read_text().splitlines() if l.strip()] if (study / 'actions.jsonl').exists() else []
    kinds = {a['action'] for a in actions}
    registry = read(study / 'configs/research_studies.json') if (study / 'configs/research_studies.json').exists() else {}
    entry = next((s for s in registry.get('studies', []) if s['id'] == sid), {})
    lock = read(study / 'configs/research_resources.lock.json')['files'] if (study / 'configs/research_resources.lock.json').exists() else []
    manifest = read(study / 'configs/research_resources.json')['resources'] if (study / 'configs/research_resources.json').exists() else []
    held_ids = {f['id'] for f in lock}
    registered, held = len(manifest), sum(1 for r in manifest if r['id'] in held_ids)
    status_doc = study / 'docs/DATASET_STATUS.md'
    counts = None
    if status_doc.exists():
        m = COUNTS.search(status_doc.read_text())
        counts = {'automatic': int(m.group(1)), 'partial': int(m.group(2)), 'action_needed': int(m.group(3)), 'no_route': int(m.group(4))} if m else None
    done = {'Registered': bool(entry) or bool(kinds & {'study_registered', 'study_created'}), 'Discovery run': 'discovery_run' in kinds,
            'Catalogue built': (study / 'data/interim/dataset_selection/latest.json').exists(), 'Status report': status_doc.exists(),
            'Downloads started': 'download_run' in kinds or bool(lock), 'All registered files held': registered > 0 and held == registered}
    missing = []
    if counts:
        missing.append(f"{counts['action_needed']} datasets need your action (registration, agreement or terms), {counts['partial']} partial, {counts['no_route']} no route")
    if registered and held < registered:
        missing.append(f'{registered - held:,} registered files not held yet')
    if not done['Discovery run']:
        missing.append('discovery not run yet')
    elif not done['Downloads started']:
        missing.append('no download run yet')
    last_change = newest_change(study / 'runs')
    return {'id': sid, 'name': entry.get('name', ''), 'milestones': [{'title': t, 'done': done[t]} for t in MILESTONES], 'done': sum(done.values()), 'total': len(MILESTONES),
            'registered_files': registered, 'held_files': held, 'held_bytes': sum(f['bytes'] for f in lock), 'reports': len(entry.get('reports', [])), 'actions': len(actions),
            'status_counts': counts, 'missing': missing, 'resolver': resolver_progress(study), 'last_change': last_change,
            'running': sid in set(running_studies), 'site': f'studies/{sid}/docs/dataset-browser/'}


def panel_model(studies_root=STUDIES, ps_output=None):
    processes = running_processes(ps_output)
    running = {p['study'] for p in processes if p['study'] and p['kind'] == 'job'}
    studies = [study_summary(p, running) for p in sorted(Path(studies_root).iterdir()) if p.is_dir() and not p.name.startswith('.') and is_study_workspace(p)] if Path(studies_root).is_dir() else []
    return {'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'processes': processes, 'studies': studies}
