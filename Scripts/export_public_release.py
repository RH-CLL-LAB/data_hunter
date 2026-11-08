#!/usr/bin/env python3
"""Assemble the code-only public release of Data Hunter into a target directory.

Copies only the tool: the site builder and its sources, the workflow scripts,
the two shared registry modules, the agent definitions and their tests. Writes
emptied registries (every schema key kept, every dataset, study, licence,
download and evidence entry removed), the release's own README, agent rules,
Compose file and requirements, creates the empty workspace state, builds the
site inside the target and optionally runs the test suites there.

Nothing from the MedScrape pipeline (compiler, proposals, pipeline steps,
benchmarks, skills), the working catalogue, research studies, licence reviews,
acquisition locks, search results or generated dataset pages is copied.

    .venv/bin/python Scripts/export_public_release.py --target ../data_hunter_release
"""
import argparse
from datetime import date
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Scripts'))
from init_empty_workspace import ensure_empty_state  # noqa: E402

TEMPLATES = ROOT / 'release'

# The tool's own code. Everything not listed here stays in the MedScrape repository.
TOOL_SCRIPTS = [
    'build_dataset_browser.py', 'init_empty_workspace.py', 'export_public_release.py',
    'register_research_study.py', 'log_study_action.py', 'review_dataset_license.py',
    'run_download_agent.py', 'run_dataset_intake_agent.py', 'acquire_research_resources.py', 'public_resources.py',
    'audit_data_licenses.py', 'audit_dataset_downloads.py', 'validate_dataset_workspace.py',
    'document_dataset_selection.py', 'audit_dataset_browser_links.py',
]
SHARED_MODULES = ['src/datahunter/__init__.py', 'src/datahunter/data_rights.py', 'src/datahunter/research_studies.py']
TOOL_TESTS = [
    'tests/test_research_studies.py', 'tests/test_dataset_license_review.py', 'tests/test_download_agent.py',
    'tests/test_dataset_intake_agent.py', 'tests/test_dataset_browser_layout.py', 'tests/test_dataset_browser_filters.py',
    'tests/test_dataset_browser_link_audit.py', 'tests/test_dataset_browser_study_ui.py', 'tests/test_dataset_documentation.py',
    'tests/test_dataset_selection.py', 'tests/test_dataset_acquisition_dates.py', 'tests/test_dataset_download_audit.py',
    'tests/test_dataset_workspace.py', 'tests/test_project_license.py', 'tests/test_docker_setup.py',
    'tests/test_public_permissions_review.py', 'tests/test_research_resource_acquisition.py',
    'tests/dataset_browser_selection.test.cjs', 'tests/dataset_license_review.test.cjs', 'tests/research_studies.test.cjs',
]
ROOT_FILES = ['LICENSE', 'CITATION.cff', '.dockerignore', '.gitattributes', 'Dockerfile',
              'data/README.md', 'runs/README.md']
INCLUDE_DIRS = ['Scripts/dataset_browser/', 'agents/']
# Release-specific files written from templates (tool-only wording, no pipeline services).
TEMPLATE_FILES = {'README.md': 'README.md', 'AGENTS.md': 'AGENTS.md', 'compose.yaml': 'compose.yaml', '.gitignore': '.gitignore', 'NOTICE': 'NOTICE',
                  'requirements.txt': 'requirements.txt', 'Scripts_README.md': 'Scripts/README.md'}

# Registries the tool reads, re-created as empty skeletons from the working copies.
EMPTY_CONFIGS = [
    'configs/dataset_selection.json', 'configs/dataset_study_usage.json', 'configs/data_licenses.json',
    'configs/data_license_assets.json', 'configs/dataset_validation.json', 'configs/dataset_license_reviews.json',
    'configs/dataset_browser_links.json', 'configs/dataset_download_reviews.json', 'configs/research_resources.json',
    'configs/research_resources.lock.json', 'configs/public_resource_acquisition.json', 'configs/public_resources.lock.json',
    'configs/public_resources.priority1.lock.json', 'configs/public_resources.remaining.lock.json',
    'configs/cbioportal_api.lock.json', 'configs/openml_parquet.lock.json', 'configs/study_fulltexts.lock.json',
    'configs/resource_scope.json', 'configs/documentation_layout.json',
    'configs/public_provider_url_corrections.json', 'configs/dataset_metadata_evidence.json',
]
# Dicts keyed by dataset, study or document identifiers: emptied rather than recursed.
ID_KEYED = {'relevance_overrides', 'structure_diagrams', 'curation_notes', 'clinical_openml', 'replacement_links',
            'shared_family_notes', 'acquisition_plan', 'task_groups', 'groups', 'families', 'dynamic_groups', 'phases',
            'preparation', 'current_download_check', 'targeted_mapping_review', 'statuses', 'review_notes'}
DATE_KEYS = {'reviewed_on', 'as_of', 'reviewed_at', 'checked_at', 'effective_on', 'updated_at', 'observed_on'}
POLICY_CONFIGS = {'configs/dataset_selection.json', 'configs/dataset_validation.json', 'configs/data_licenses.json'}
NEUTRAL = 'Empty registry in the code-only release; filled by the workflow scripts.'
DEFAULT_STUDY = {'id': 'baseline', 'name': 'Baseline study', 'question': 'Which datasets support this project’s research question?',
                 'intended_use': 'Describe the intended data and AI use before searching for datasets.',
                 'population': '', 'targets': '', 'inclusion': '', 'exclusion': '',
                 'dataset_ids': [], 'dataset_annotations': {}, 'study_annotations': {}}


def skeleton(value, key=None, today=''):
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        if key in ID_KEYED:
            return {}
        return {k: skeleton(v, k, today) for k, v in value.items()}
    if key in DATE_KEYS and isinstance(value, str):
        return today
    return value


def tracked_files():
    out = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True, capture_output=True).stdout
    return [p for p in out.decode().split('\0') if p]


def selected_files(files):
    wanted = set(ROOT_FILES) | set(SHARED_MODULES) | set(TOOL_TESTS) | {'Scripts/' + s for s in TOOL_SCRIPTS}
    for path in files:
        if path in wanted or path.startswith(tuple(INCLUDE_DIRS)):
            yield path


def write_docs_index(target):
    (target / 'docs/README.md').write_text(
        '# Documentation\n\n'
        'The site in `dataset-browser/` is the documentation of Data Hunter: start with its Tutorial page.\n\n'
        '- `DATASET_SELECTION.md`, `DATASET_ACTION_PRIORITIES.md` and `DATA_LICENSES_AND_AI_USE.md` are generated by '
        '`Scripts/document_dataset_selection.py` and `Scripts/audit_data_licenses.py`; they start empty.\n'
        '- `research-studies/<study-id>/` holds each registered study’s reports and its append-only `actions.jsonl`.\n')


def export(target, run_tests=True):
    target = Path(target).resolve()
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f'target_not_empty: {target}')
    target.mkdir(parents=True, exist_ok=True)
    files = list(selected_files(tracked_files()))
    for rel in files:
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dest)
    for template, dest_rel in TEMPLATE_FILES.items():
        dest = target / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATES / template, dest)
    today = date.today().isoformat()
    for rel in EMPTY_CONFIGS:
        source = ROOT / rel
        if not source.exists():
            continue
        value = skeleton(json.loads(source.read_text()), today=today)
        if rel not in POLICY_CONFIGS:
            # Review narratives describe the working catalogue; the release gets a neutral description.
            for key in ('scope', 'method', 'reason', 'evidence', 'permission', 'purpose'):
                if isinstance(value.get(key), str):
                    value[key] = NEUTRAL
        if rel == 'configs/dataset_browser_links.json':
            value['evidence'] = ''
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    (target / 'configs/research_studies.json').write_text(json.dumps({'schema_version': 1, 'default_study': DEFAULT_STUDY['id'], 'studies': [DEFAULT_STUDY]}, indent=2, ensure_ascii=False) + '\n')
    (target / 'docs').mkdir(exist_ok=True)
    write_docs_index(target)
    written = ensure_empty_state(target)
    python = sys.executable
    build = subprocess.run([python, 'Scripts/build_dataset_browser.py'], cwd=target, capture_output=True, text=True)
    result = {'target': str(target), 'files_copied': len(files) + len(TEMPLATE_FILES), 'registries_emptied': len(EMPTY_CONFIGS),
              'state_written': [str(p.relative_to(target)) for p in written], 'build_ok': build.returncode == 0,
              'build_tail': build.stdout[-400:] + build.stderr[-1200:]}
    if run_tests and build.returncode == 0:
        subprocess.run(['git', 'init', '-q'], cwd=target, capture_output=True)
        tests = subprocess.run([python, '-m', 'unittest', 'discover', '-s', 'tests'], cwd=target, capture_output=True, text=True)
        result['tests_ok'] = tests.returncode == 0
        result['tests_tail'] = tests.stderr[-1500:]
        node = subprocess.run('for f in tests/*.cjs; do node --test "$f" || exit 1; done', shell=True, cwd=target, capture_output=True, text=True)
        result['node_tests_ok'] = node.returncode == 0
        shutil.rmtree(target / '.git', ignore_errors=True)
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--skip-tests', action='store_true')
    args = parser.parse_args()
    export(args.target, not args.skip_tests)
