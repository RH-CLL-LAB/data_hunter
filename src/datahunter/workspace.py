"""Where a study's files live: one folder per study under studies/<id>/.

The code (Scripts/, src/, agents/, tests/) lives at CODE_ROOT. Everything a study
produces lives in its own workspace folder:

    studies/<id>/
      study.json       marker and creation record for this workspace
      actions.jsonl    append-only action log of the study
      reports/         tracked Markdown reports (discovery, reviews)
      configs/         this study's registries (datasets, licences, locks, reviews)
      data/            retained notices, quarantine, acquisitions, receipts
      runs/            intake, download, licence-review and registration runs
      docs/            generated catalogue documents and the dataset-browser site

The active study is chosen with `Scripts/study.py use <id>` (stored in
studies/.active) or the DATA_HUNTER_STUDY environment variable. A whole other
folder can be used as the workspace with DATA_HUNTER_WORKSPACE. When no study
exists yet, the site can be built from a bootstrap workspace (studies/.bootstrap)
so a fresh install has a tutorial and a New-study form to start from.
"""
from datetime import datetime, timezone
import json
import os
import re
import shutil
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[2]
STUDIES = CODE_ROOT / 'studies'
ACTIVE = STUDIES / '.active'
TEMPLATE = CODE_ROOT / 'templates/study'
BOOTSTRAP_ID = '.bootstrap'
ID_PATTERN = re.compile(r'[a-z0-9][a-z0-9_-]{0,79}')
WORKSPACE_DIRS = ('configs', 'data', 'runs', 'docs', 'reports')
BOOTSTRAP_STUDY = {'id': 'baseline', 'name': 'Baseline study',
                   'question': 'Which datasets support this project’s research question?',
                   'intended_use': 'Describe the intended data and AI use before searching for datasets.',
                   'population': '', 'targets': '', 'inclusion': '', 'exclusion': '',
                   'dataset_ids': [], 'dataset_annotations': {}, 'study_annotations': {}}
NO_STUDY_MESSAGE = ('no_active_study: create one from the site’s New study form and register its export with '
                    '"Scripts/register_research_study.py --request <file> --apply", or choose one with "Scripts/study.py use <id>"')


def is_study_workspace(path):
    return (Path(path) / 'study.json').is_file()


def valid_id(study_id):
    return isinstance(study_id, str) and bool(ID_PATTERN.fullmatch(study_id))


def study_path(study_id):
    if study_id != BOOTSTRAP_ID and not valid_id(study_id):
        raise ValueError('invalid_research_study_id')
    return STUDIES / study_id


def active_study_id():
    value = os.environ.get('DATA_HUNTER_STUDY')
    if not value:
        try:
            value = ACTIVE.read_text().strip()
        except OSError:
            return None
    return value or None


def set_active(study_id):
    folder = study_path(study_id)
    if not is_study_workspace(folder):
        raise ValueError('unknown_research_study_workspace: ' + str(folder))
    STUDIES.mkdir(exist_ok=True)
    ACTIVE.write_text(study_id + '\n')
    return folder


def clear_active(study_id=None):
    if study_id is None or active_study_id() == study_id:
        ACTIVE.unlink(missing_ok=True)


def list_studies():
    if not STUDIES.is_dir():
        return []
    return sorted(p.name for p in STUDIES.iterdir() if is_study_workspace(p) and not p.name.startswith('.'))


def workspace_root(default=None, bootstrap=False):
    """The active workspace. Resolution order: DATA_HUNTER_WORKSPACE, the active study,
    a legacy self-contained checkout (configs/ next to the code), the bootstrap workspace
    when `bootstrap` is set, then `default`; with none of these it exits with guidance."""
    env = os.environ.get('DATA_HUNTER_WORKSPACE')
    if env:
        return Path(env).resolve()
    study = active_study_id()
    if study:
        folder = study_path(study)
        if is_study_workspace(folder):
            return folder
        if os.environ.get('DATA_HUNTER_STUDY'):
            raise SystemExit('active_study_workspace_missing: ' + str(folder))
        # The pointer names a folder that was deleted by hand: drop it and carry on as if no study were active.
        print('warning: active study folder missing, pointer cleared: ' + str(folder), file=sys.stderr)
        clear_active(study)
    if (CODE_ROOT / 'configs/research_studies.json').is_file():
        return CODE_ROOT
    if bootstrap:
        return ensure_bootstrap()
    if default is not None:
        return Path(default)
    raise SystemExit(NO_STUDY_MESSAGE)


def require_workspace(root):
    """Scripts call this before touching registries so a missing study fails with guidance."""
    root = Path(root)
    if not (root / 'configs/research_studies.json').is_file():
        raise SystemExit(NO_STUDY_MESSAGE)
    return root


def workspace_study_id(root):
    """The study a workspace folder belongs to, or None for a legacy multi-study root."""
    marker = Path(root) / 'study.json'
    if not marker.is_file():
        return None
    try:
        return json.loads(marker.read_text()).get('id')
    except ValueError:
        return None


def create_study_workspace(study_id, registry=None):
    """Create studies/<id>/ with empty registries; returns the folder. Existing workspaces are kept."""
    folder = study_path(study_id)
    if is_study_workspace(folder):
        return folder
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('folder_exists_but_is_not_a_study_workspace: ' + str(folder))
    for name in WORKSPACE_DIRS:
        (folder / name).mkdir(parents=True, exist_ok=True)
    for source in sorted((TEMPLATE / 'configs').glob('*.json')):
        shutil.copyfile(source, folder / 'configs' / source.name)
    registry = registry or {'schema_version': 1, 'default_study': study_id, 'studies': []}
    (folder / 'configs/research_studies.json').write_text(json.dumps(registry, indent=2, ensure_ascii=False) + '\n')
    (folder / 'study.json').write_text(json.dumps({'schema_version': 1, 'id': study_id,
                                                   'created_at': datetime.now(timezone.utc).isoformat()}, indent=2) + '\n')
    (folder / 'docs/README.md').write_text(
        '# Generated documents\n\nThe catalogue documents and the `dataset-browser/` site in this folder are generated '
        'from the study’s registries by the workflow scripts. Edit the registries, not these files.\n')
    sys.path.insert(0, str(CODE_ROOT / 'Scripts'))
    from init_empty_workspace import ensure_empty_state  # noqa: E402
    ensure_empty_state(folder)
    return folder


def ensure_bootstrap():
    """A workspace with no study of its own: lets a fresh install build and open the site."""
    folder = STUDIES / BOOTSTRAP_ID
    if not is_study_workspace(folder):
        create_study_workspace(BOOTSTRAP_ID, {'schema_version': 1, 'default_study': BOOTSTRAP_STUDY['id'], 'studies': [BOOTSTRAP_STUDY]})
    return folder


def site_dir(root):
    return Path(root) / 'docs/dataset-browser'


def snapshot_path(path, root):
    """Evidence record for a hashed file: workspace files by relative path, tool code marked root='code'."""
    path, root = Path(path).resolve(), Path(root).resolve()
    if path.is_relative_to(root):
        return {'path': path.relative_to(root).as_posix()}
    if path.is_relative_to(CODE_ROOT):
        return {'path': path.relative_to(CODE_ROOT).as_posix(), 'root': 'code'}
    raise ValueError('evidence_outside_workspace_and_code: ' + str(path))


def resolve_snapshot(item, root):
    return (CODE_ROOT if item.get('root') == 'code' else Path(root)) / item['path']


def snapshot_copy_path(item):
    """Where a snapshot copy goes inside a run folder: code files under code/, workspace files as they are."""
    return Path('code') / item['path'] if item.get('root') == 'code' else Path(item['path'])
