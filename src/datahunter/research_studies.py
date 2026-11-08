"""Research-project briefs are distinct from published-paper catalogue records."""
import hashlib
import json
import re
from pathlib import Path

try:
    from .workspace import workspace_root, workspace_study_id
except ImportError:  # loaded as a plain file, outside the package
    from workspace import workspace_root, workspace_study_id


def default_study(root=None):
    """The workspace's own study id from configs/research_studies.json ('default_study'); 'baseline' when unset."""
    path = Path(root or workspace_root(default=Path(__file__).resolve().parents[2])) / 'configs/research_studies.json'
    try:
        return json.loads(path.read_text()).get('default_study') or 'baseline'
    except (OSError, ValueError):
        return 'baseline'


DEFAULT_STUDY = default_study()

def normalize(value):
    if not isinstance(value, dict) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', value.get('id', '')):
        raise ValueError('invalid_research_study_id')
    result = {'id': value['id']}
    for key in ['name', 'question', 'intended_use']:
        text = value.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > (160 if key == 'name' else 4000):
            raise ValueError('invalid_research_study_' + key)
        result[key] = text.strip()
    for key in ['population', 'targets', 'inclusion', 'exclusion']:
        text = value.get(key, '')
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError('invalid_research_study_' + key)
        result[key] = text.strip()
    return result

def fingerprint(value):
    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

def registered(root, study_id):
    path = Path(root) / 'configs/research_studies.json'
    if not path.is_file():
        return None
    records = json.loads(path.read_text())['studies']
    matches = [r for r in records if r['id'] == study_id]
    if len(matches) > 1:
        raise ValueError('duplicate_research_study_id')
    return normalize(matches[0]) if matches else None


# Per-study action log: append-only JSONL tracked with the study's reports.
BROWSER_ACTIONS = {
    'study_created', 'study_imported', 'study_exported', 'shortlist_saved', 'annotation_saved',
    'intake_prompt_prepared', 'download_instructions_prepared', 'file_task_prepared',
    'file_inventory_exported', 'licence_review_prepared', 'note',
}
# Written only by the scripts that perform them; a browser export can never claim one.
EXECUTED_ACTIONS = {
    'study_registered', 'study_updated', 'study_removed', 'discovery_run', 'report_added',
    'download_run', 'licence_review_registered', 'file_task_run',
}
LOG_ACTIONS = BROWSER_ACTIONS | EXECUTED_ACTIONS


def study_dir(root, study_id):
    """A study workspace (studies/<id>/) holds its own study at the top; any other id keeps the legacy nested folder."""
    normalize({'id': study_id, 'name': 'x', 'question': 'x', 'intended_use': 'x'})  # validates the ID
    root = Path(root)
    if workspace_study_id(root) == study_id:
        return root
    return root / 'docs/research-studies' / study_id


def reports_dir(root, study_id):
    folder = study_dir(root, study_id)
    return folder / 'reports' if folder == Path(root) else folder


def log_path(root, study_id):
    return study_dir(root, study_id) / 'actions.jsonl'


def validate_log_event(event):
    """Return a cleaned event or raise; events are data, never commands."""
    if not isinstance(event, dict) or event.get('action') not in LOG_ACTIONS:
        raise ValueError('invalid_study_log_action')
    at = event.get('at')
    if not isinstance(at, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.]+(Z|[+-]\d{2}:\d{2})', at):
        raise ValueError('invalid_study_log_time')
    cleaned = {'at': at, 'action': event['action']}
    for key, limit in [('detail', 1000), ('source', 80), ('run', 200), ('actor', 80)]:
        text = event.get(key)
        if text is None:continue
        if not isinstance(text, str) or len(text) > limit:raise ValueError('invalid_study_log_' + key)
        cleaned[key] = text
    return cleaned


def read_log(root, study_id):
    path = log_path(root, study_id)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []


def append_log(root, study_id, events):
    """Append new events (deduplicated by time, action and detail); never rewrites earlier lines."""
    path = log_path(root, study_id); existing = read_log(root, study_id)
    seen = {(e['at'], e['action'], e.get('detail', '')) for e in existing}
    fresh = []
    for event in events:
        cleaned = validate_log_event(event); key = (cleaned['at'], cleaned['action'], cleaned.get('detail', ''))
        if key not in seen:seen.add(key);fresh.append(cleaned)
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            for event in sorted(fresh, key=lambda e: e['at']):stream.write(json.dumps(event, ensure_ascii=False) + '\n')
    return fresh
