"""Fail-closed data-license checks before workflow processing and AI transport.

License decisions are host-reviewed configuration, never model-generated facts.
Public availability, an input authorization, and a code license are not data rights.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from functools import wraps
from hashlib import sha256
import json
import inspect
import os
from pathlib import Path
import sys
try:
    from datahunter.research_studies import DEFAULT_STUDY, default_study, registered as registered_study, fingerprint as study_fingerprint
except ModuleNotFoundError as error:  # loaded as a plain file, outside the package
    if error.name != 'datahunter':
        raise
    from research_studies import DEFAULT_STUDY, default_study, registered as registered_study, fingerprint as study_fingerprint

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / 'configs/data_licenses.json'
ASSETS = ROOT / 'configs/data_license_assets.json'
OPERATIONS = {'local_inference', 'local_embedding', 'remote_inference',
              'remote_embedding', 'training', 'redistribution'}
MODES = {'restrictive', 'lenient'}
_session = ContextVar('datahunter_data_license_session', default=None)
# The historical pipeline also imports sibling modules as top-level names.
if __name__ in {'data_rights', 'medscrape.data_rights'}:
    sys.modules.setdefault('data_rights', sys.modules[__name__])
    sys.modules.setdefault('medscrape.data_rights', sys.modules[__name__])


class DataRightsError(ValueError):
    def __init__(self, report):
        self.report = report
        reasons = sorted({d['reason'] for d in report.get('decisions', []) if d['state'] != 'allowed'})
        super().__init__('data_license_blocked: ' + '; '.join(reasons or ['missing_preflight']))


def file_digest(path):
    h = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_license_policy_key')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs)


class LicenseRegistry:
    def __init__(self, policy=None, assets=None, *, root=None, mode=None, study_id=None):
        self.root = Path(root or ROOT).resolve()
        self.policy_path, self.assets_path = Path(policy or POLICY), Path(assets or ASSETS)
        self.policy, asset_document = _read(self.policy_path), _read(self.assets_path)
        session = _session.get()
        self.default_study = default_study(self.root)
        self.study_id = study_id or (session[0].study_id if session else os.environ.get('DATA_HUNTER_STUDY_ID') or os.environ.get('MEDSCRAPE_STUDY_ID', self.default_study))
        profile = registered_study(self.root, self.study_id)
        self.study_hash = study_fingerprint(profile) if profile else None
        self.mode = mode or (session[0].mode if session else
                             (os.environ.get('DATA_HUNTER_LICENSE_MODE') or os.environ.get('MEDSCRAPE_DATA_LICENSE_MODE') or self.policy.get('default', 'lenient')))
        if self.mode not in MODES:
            raise ValueError('unknown_data_license_mode')
        if self.policy.get('schema_version') != 1 or asset_document.get('schema_version') != 1:
            raise ValueError('unsupported_data_license_schema')
        self.policy_hash, self.assets_hash = file_digest(self.policy_path), file_digest(self.assets_path)
        self.resources = {r['id']: r for r in self.policy['resources']}
        if len(self.resources) != len(self.policy['resources']):
            raise ValueError('duplicate_data_license_resource')
        self.assets = asset_document['files']
        if any(a['resource'] not in self.resources for a in self.assets):
            raise ValueError('unregistered_asset_license')
        self.by_hash = {}
        self.by_path = {}
        for asset in self.assets:
            self.by_hash.setdefault(asset['sha256'], set()).add(asset['resource'])
            previous = self.by_path.setdefault(asset['path'], asset)
            if previous != asset:
                raise ValueError('conflicting_asset_license')

    def _decision(self, resource, operation):
        r = self.resources[resource]
        original = r
        overlay = r.get('study_reviews', {}).get(self.study_id)
        study_matches = r.get('study_id', self.default_study) == self.study_id
        if overlay and overlay.get('study_sha256') == self.study_hash and self.study_hash:
            r = {**r, **overlay}
            study_matches = True
        state = r['operations'].get(operation, 'unreviewed')
        reason = r['reason'] if state != 'allowed' else 'license_conditions_recorded'
        # AI permission and underlying access/use rights are separate decisions.
        # A broad software/data grant is not an explicit AI-specific approval.
        ai_permission = r.get('ai_permissions', {}).get(operation, 'unreviewed')
        if ai_permission not in {'allowed', 'prohibited', 'unreviewed'}:
            raise ValueError('unknown_ai_permission_state')
        ai_state = ('prohibited' if ai_permission == 'prohibited' else
                    'allowed' if self.mode == 'lenient' or ai_permission == 'allowed'
                    or operation == 'redistribution' else 'unreviewed')
        if ai_state == 'prohibited':
            state, reason = 'prohibited', 'explicit_ai_prohibition'
        elif state == 'allowed' and ai_state != 'allowed':
            state, reason = 'unreviewed', 'explicit_ai_approval_not_recorded'
        # A conditional ban cannot be discarded merely because the run has not
        # supplied enough context to determine whether its condition applies.
        if state == 'allowed' and any(operation in p.get('operations', [])
                                      for p in r.get('conditional_prohibitions', [])):
            state, reason = 'unreviewed', 'conditional_prohibition_scope_requires_review'
        if original.get('ai_permissions', {}).get(operation) == 'prohibited':
            state, reason = 'prohibited', 'explicit_ai_prohibition'
        elif original['operations'].get(operation) == 'prohibited':
            state, reason = 'prohibited', original['reason']
        if (not r.get('reviewed_on') or date.fromisoformat(r['reviewed_on']) > date.today()
                or date.fromisoformat(r['review_due']) < date.today()):
            state, reason = 'unreviewed', 'license_review_missing_or_stale'
        evidence = r.get('evidence', [])
        if state == 'allowed' and not evidence:
            state, reason = 'unreviewed', 'license_evidence_missing'
        for item in evidence:
            path = self.root / item['snapshot']
            if (not path.resolve().is_relative_to(self.root) or path.is_symlink()
                    or not path.is_file() or file_digest(path) != item['sha256']):
                state, reason = 'unreviewed', 'license_evidence_missing_or_changed'
        # A decision recorded for one study never applies to another one, including the default study.
        if state != 'prohibited' and not study_matches:
            state, reason = 'unreviewed', 'permission_recorded_for_another_study'
        if state != 'prohibited' and self.study_id != self.default_study and (not study_matches or not self.study_hash or
                r.get('study_sha256', r.get('manual_review', {}).get('study_sha256')) != self.study_hash):
            state, reason = 'unreviewed', 'study_specific_permission_review_required'
        return {'resource': resource, 'operation': operation, 'state': state, 'reason': reason,
                'study_id': self.study_id, 'study_sha256': self.study_hash,
                'mode': self.mode, 'ai_permission': ai_permission, 'ai_criterion': ai_state,
                'underlying_use_permission': r['operations'].get(operation, 'unreviewed'),
                'conditional_prohibitions': r.get('conditional_prohibitions', []),
                'license': r['license'], 'conditions': r.get('conditions', []),
                'sources': r.get('sources', [])}

    def check(self, fingerprints, operations):
        operations = sorted(set(operations))
        if not operations or not set(operations) <= OPERATIONS:
            raise ValueError('unknown_or_empty_data_operation')
        fingerprints = list(fingerprints)
        decisions, resources = [], set()
        for value in fingerprints:
            matched = self.by_hash.get(value, set())
            if not matched:
                decisions.append({'resource': 'unregistered_input', 'operation': ','.join(operations),
                                  'state': 'unreviewed', 'reason': 'input_hash_has_no_reviewed_license'})
            resources.update(matched)
        if not fingerprints:
            decisions.append({'resource': 'empty_input', 'operation': ','.join(operations),
                              'state': 'unreviewed', 'reason': 'no_identified_source_artifacts'})
        for resource in sorted(resources):
            decisions.extend(self._decision(resource, operation) for operation in operations)
        return {'schema_version': 1, 'mode': self.mode, 'study_id': self.study_id, 'study_sha256': self.study_hash, 'status': 'allowed' if decisions and all(
                    d['state'] == 'allowed' for d in decisions) else 'blocked',
                'checked_on': date.today().isoformat(), 'policy_sha256': self.policy_hash,
                'assets_sha256': self.assets_hash, 'source_sha256': sorted(set(fingerprints)),
                'decisions': decisions, 'model_calls': 0}

    def paths(self, paths, operations):
        files = []
        for source in paths:
            source = Path(source)
            if not source.exists() or source.is_symlink():
                raise ValueError('license_source_missing_or_symlink')
            candidates = sorted(source.rglob('*')) if source.is_dir() else [source]
            for path in candidates:
                if path.is_symlink():
                    raise ValueError('license_input_symlink')
                if path.is_file():
                    files.append((str(path.resolve()), file_digest(path)))
        report = self.check([value for _, value in files], operations)
        report['input_files'] = len(files)
        report['artifacts'] = [{'path': path, 'sha256': value, 'resources': sorted(self.by_hash.get(value, set()))}
                               for path, value in files]
        return report


@contextmanager
def licensed_use(report, registry=None):
    """Recompute permission from evidence; a copied 'allowed' report cannot grant it."""
    registry = registry or LicenseRegistry()
    if report.get('study_id', registry.default_study) != registry.study_id:
        raise DataRightsError({'status':'blocked','decisions':[{'state':'unreviewed','reason':'study_context_mismatch'}]})
    operations = {d['operation'] for d in report['decisions']}
    current = registry.check(report['source_sha256'], operations)
    if current['status'] != 'allowed':
        raise DataRightsError(current)
    token = _session.set((registry, current))
    try:
        yield current
    finally:
        _session.reset(token)


def assert_ai_allowed(operation):
    session = _session.get()
    if session is None:
        raise DataRightsError({'status': 'blocked', 'decisions': []})
    registry, report = session
    fresh = LicenseRegistry(registry.policy_path, registry.assets_path, root=registry.root, mode=registry.mode, study_id=registry.study_id)
    if fresh.policy_hash != report['policy_sha256'] or fresh.assets_hash != report['assets_sha256']:
        raise DataRightsError({'status': 'blocked', 'decisions': [
            {'state': 'unreviewed', 'reason': 'license_policy_changed_during_run'}]})
    if fresh.study_hash != report.get('study_sha256'):
        raise DataRightsError({'status':'blocked','decisions':[{'state':'unreviewed','reason':'study_brief_changed_during_run'}]})
    if operation not in {d['operation'] for d in report['decisions']}:
        raise DataRightsError({'status': 'blocked', 'decisions': [
            {'state': 'unreviewed', 'reason': 'operation_not_preflighted'}]})
    checked = fresh.check(report['source_sha256'], [operation])
    if checked['status'] != 'allowed':
        raise DataRightsError(checked)
    return checked


def catalog_report(catalog, operations, annotations=(), registry=None):
    fingerprints = [table.sha256 for table in catalog.tables.values()]
    fingerprints.extend(a.artifact_sha256 for a in annotations)
    return (registry or LicenseRegistry()).check(fingerprints, operations)


def license_session_active():
    return _session.get() is not None


def knowledge_disclosure_allowed(registry, release_id, operation='local_inference'):
    """Intersect existing per-concept rights with this audit's release decision."""
    session = _session.get()
    if session is None:
        return False
    license_registry, _ = session
    release = next((r for r in registry.manifest.releases if r.id == release_id), None)
    resource = 'knowledge_' + release.family if release is not None else None
    if resource not in license_registry.resources:
        return False
    return license_registry._decision(resource, operation)['state'] == 'allowed'


def licensed_catalog(operation):
    """Production model clients require a source-bound license session.

    Scripted test doubles make no network calls. The HTTP boundary independently
    denies any real inference request made without a license session.
    """
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            client, catalog = bound.arguments['client'], bound.arguments['catalog']
            if not getattr(client, 'requires_data_license', False):
                return function(*args, **kwargs)
            report = catalog_report(catalog, [operation], bound.arguments.get('annotations', ()))
            if report['status'] != 'allowed':
                raise DataRightsError(report)
            with licensed_use(report):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def licensed_pipeline(function):
    @wraps(function)
    def wrapped(config_module, args):
        factories = [sys.modules.get(name) for name in ('llm_client', 'medscrape.llm_client')]
        local = any(getattr(m, '_local_client', None) is not None for m in factories if m)
        operations = ['local_inference', 'local_embedding'] if local else ['remote_inference', 'remote_embedding']
        paths = [config_module.SOURCE_DATA_FOLDER]
        dictionary = getattr(config_module, 'DATASHEET_FOLDER', None)
        if dictionary and Path(dictionary).exists():
            paths.append(dictionary)
        registry = LicenseRegistry()
        report = registry.paths(paths, operations)
        output = Path(config_module.OUTPUT_BASE_FOLDER) / 'license_preflight' / config_module.DATA_SOURCE_NAME
        output.mkdir(parents=True, exist_ok=True)
        (output / 'data-license-preflight.json').write_text(json.dumps(report, indent=2) + '\n')
        if report['status'] != 'allowed':
            raise DataRightsError(report)
        with licensed_use(report, registry):
            return function(config_module, args)
    return wrapped


class GuardedProvider:
    """Guard the legacy SDK's model operations, including standalone stage calls."""
    def __init__(self, provider, operation=None):
        self.provider, self.operation = provider, operation

    def __getattr__(self, name):
        value = getattr(self.provider, name)
        if name in {'chat', 'completions', 'responses', 'embeddings', 'beta', 'batches', 'files', 'fine_tuning', 'jobs'}:
            operation = ('remote_embedding' if name == 'embeddings' else
                         'training' if name in {'fine_tuning', 'jobs'} else self.operation or 'remote_inference')
            return GuardedProvider(value, operation)
        if name in {'create', 'parse', 'stream', 'upload', 'upload_and_poll'} and callable(value):
            @wraps(value)
            def checked(*args, **kwargs):
                assert_ai_allowed(self.operation or 'remote_inference')
                if self.operation == 'training':
                    assert_ai_allowed('remote_inference')
                if kwargs.get('purpose') in {'fine-tune', 'fine_tuning'}:
                    assert_ai_allowed('training')
                return value(*args, **kwargs)
            return checked
        return value
