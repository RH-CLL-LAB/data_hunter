"""Licence review presentation: runtime decisions remain authoritative."""
from datetime import date
from functools import lru_cache
from hashlib import sha256
from html import escape
import json
from pathlib import Path

try:
    from .filter_metadata import FACETS
    from .hints import help_button
except ImportError:  # imported as a plain module from Scripts/dataset_browser
    from filter_metadata import FACETS
    from hints import help_button
LABELS = FACETS['license_status'][1]  # one wording for pages, rows, filters and scripts

def status(dataset, reviews=(), root=None):
    scopes = dataset.get('input_scopes', [])
    reports = [s['permission_preflight'] for s in scopes] if scopes else [dataset['associated_download_permissions']]
    decisions = [decision for report in reports for decision in report.get('decisions', [])]
    if any(d.get('state') == 'prohibited' for d in decisions):
        return 'prohibited'
    if reports and decisions and all(r.get('status') == 'allowed' for r in reports):
        return 'approved'
    refusals = [r for r in reviews if r['dataset_id'] == dataset['id'] and r.get('applied') and
                r.get('review_due', '') >= date.today().isoformat() and
                {'local_inference', 'local_embedding'} & set(r.get('operations', []))]
    if refusals and refusals[-1]['status'] == 'prohibited':
        return 'prohibited'
    applicable = [r for r in reviews if r['dataset_id'] == dataset['id'] and
                  r.get('applied') and r.get('review_due', '') >= date.today().isoformat() and
                  {'local_inference', 'local_embedding'} <= set(r.get('operations', []))]
    if applicable and applicable[-1]['status'] == 'approved_source_pending_identity':
        evidence = applicable[-1].get('evidence', [])
        if root and evidence and all((root / e['snapshot']).is_file() and
                (root / e['snapshot']).resolve().is_relative_to(root) and
                sha256((root / e['snapshot']).read_bytes()).hexdigest() == e['sha256'] for e in evidence):
            return 'source_reviewed'
    return 'review_required'

@lru_cache(maxsize=None)
def fingerprint(path):
    h = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def form(dataset, root, catalogue_sha256, policy_sha256, assets_sha256, state, notices=()):
    e = lambda value: escape(str(value), quote=True)
    assets = {}
    for key in ['obtained_artifacts', 'prepared_artifact_profiles']:
        for item in dataset.get(key, []):
            path = root / item['path']
            if path.is_file() and path.resolve().is_relative_to(root) and not path.is_symlink():
                assets[item['path']] = {'path': item['path'], 'sha256': item.get('sha256') or fingerprint(path)}
    context = {'dataset_id': dataset['id'], 'catalogue_sha256': catalogue_sha256, 'policy_sha256': policy_sha256,
               'assets_sha256': assets_sha256, 'assets': list(assets.values()), 'notices': list(notices)}
    tone = {'approved': 'approved', 'review_required': 'review', 'prohibited': 'prohibited', 'source_reviewed': 'ref'}.get(state, 'review')
    html = '<div class="license-status tone-'+tone+'" data-permission-banner><strong>Local AI permission: '+e(LABELS[state])+'</strong><p class="small">This is separate from download completeness and benchmark readiness. Approval covers only the recorded source scope and operations; it does not clear a full study or online AI service.</p></div>'
    html += '<details class="manual-license-review"><summary>Review licence and record decision</summary>'+help_button('help-license-review','the licence review form','Record your reading of the exact terms: the verdict, the operations and held files it covers, and the retained notice as evidence. Generate the decision, then hand the prompt or JSON to the agent, which validates and registers it. A decision can never override an explicit prohibition, supply a missing agreement or turn silence into approval; saving the form grants nothing by itself.')+'<p>Read the attached notices and exact release terms above. Record your interpretation below. This static page saves a decision for Claude Code or Codex to validate and register; saving it does not itself clear AI use.</p><form data-license-review-form>'
    html += '<label>Decision<select name="verdict"><option value="needs_clarification">Needs clarification</option><option value="approve">Approve the specified use</option><option value="prohibited">Prohibited for the specified use</option></select></label>'
    if notices:
        html += '<label>Use a retained notice to fill the evidence fields<select name="retained_notice"><option value="">Choose a notice after reading its scope</option>'+''.join('<option value="'+str(i)+'">'+e(n['label'])+'</option>' for i,n in enumerate(notices))+'</select></label><p class="small">This fills evidence details only. It does not approve the data or establish that a paper/software notice covers clinical tables.</p>'
    for name, label in [('reviewer', 'Reviewer name'), ('source_version', 'Exact source / release'), ('license_name', 'Applicable data licence'), ('purpose', 'Intended research purpose'), ('scope', 'Data/components covered, including archive contents'), ('rationale', 'Decision reasoning and relevant licence clauses')]:
        html += '<label>'+label+'<input name="'+name+'" required maxlength="4000"></label>'
    html += '<div class="review-dates"><label>Reviewed on<input type="date" name="reviewed_on" required data-default-today></label><label>Review due<input type="date" name="review_due" required></label></div>'
    html += '<fieldset><legend>Operations actually reviewed</legend>'
    for op, label in [('local_inference', 'Local inference'), ('local_embedding', 'Local embeddings'), ('remote_inference', 'Online inference / provider disclosure'), ('remote_embedding', 'Online embeddings / provider disclosure'), ('training', 'Model training'), ('redistribution', 'Redistribution')]:
        html += '<label class="checkoption"><input type="checkbox" name="review_operations" value="'+op+'"><span>'+label+'</span></label>'
    html += '</fieldset><label>What does the source say about the selected AI operations?<select name="ai_evidence"><option value="silent">No explicit applicable approval or prohibition found</option><option value="explicit_approval">Explicit source approval — cite the clause below</option><option value="explicit_prohibition">Explicit applicable prohibition</option></select></label><label>AI-specific evidence / clause<textarea name="ai_evidence_note" placeholder="Required for explicit approval or prohibition; cite the retained notice."></textarea></label>'
    html += '<label>Official data licence / agreement URL<input name="evidence_url" type="url" placeholder="https://…" required></label><label>Retained licence notice — project-relative path<input name="evidence_path" placeholder="data/interim/license_audit/…/notice.txt" required></label><label>Retained notice SHA-256<input name="evidence_sha256" pattern="[a-f0-9]{64}" required></label><p class="small">If the notice has not been retained, ask the agent to save and hash it before completing this review. Never paste a private agreement into an online service.</p><label>Conditions to retain (one per line)<textarea name="conditions" placeholder="Attribution, noncommercial purpose, modification/distribution restrictions, approved compute environment…"></textarea></label>'
    html += '<fieldset><legend>Exact held files covered by this decision</legend><p class="small">Tick only files whose complete contents you reviewed rights for. A shared archive can contain multiple studies and licences. No selected files means source review only: files must still be registered after acquisition, and no input becomes AI-ready.</p>'
    for item in assets.values():
        html += '<label class="checkoption"><input type="checkbox" name="review_assets" value="'+e(item['path'])+'"><span>'+e(item['path'])+'<small class="cellsub">SHA-256: '+item['sha256']+'</small></span></label>'
    html += '</fieldset>'
    for key, label in [('underlying_rights_confirmed', 'The applicable data rights permit the selected purpose and operations.'), ('conditions_satisfied', 'I have checked the recorded attribution, modification, privacy and provider conditions.'), ('agreements_complete', 'All required registrations, approvals and agreements are in place for this use.'), ('whole_artifacts_reviewed', 'My decision covers every selected artifact in full, including third-party components and shared archive contents.')]:
        html += '<label class="checkoption"><input type="checkbox" name="'+key+'"><span>'+label+'</span></label>'
    html += '<p class="small">Manual interpretation cannot override an explicit prohibition or supply a missing agreement. AI-silent terms may pass lenient mode once underlying rights are approved; strict mode still requires explicit source approval.</p><div class="selectionbuttons"><button class="agent-save" type="submit" data-generate-license-review>Generate review decision</button><button class="plainbutton" type="button" data-copy-license-review disabled>Copy decision prompt</button><button class="plainbutton" type="button" data-save-license-review disabled>Save decision for Claude Code or Codex (.json)</button></div><p role="status" data-license-review-status></p></form><details hidden data-license-review-preview-box><summary>Decision for Claude Code or Codex — pending registration</summary><pre data-license-review-preview></pre></details></details>'
    html += '<script type="application/json" id="license-review-context">'+json.dumps(context).replace('<', '\\u003c')+'</script>'
    return html
