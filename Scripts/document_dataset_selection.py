#!/usr/bin/env python3
"""Consolidate selection facts without changing dataset bytes or readiness gates."""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import csv
import json
import os
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root, snapshot_path, snapshot_copy_path
ROOT = workspace_root(default=CODE_ROOT)
from datahunter.data_rights import file_digest

DOC = ROOT / 'docs/DATASET_SELECTION.md'
BASE = ROOT / 'data/interim/dataset_selection'
POLICY = ROOT / 'configs/dataset_selection.json'
USAGE = ROOT / 'configs/dataset_study_usage.json'
FULLTEXTS = ROOT / 'data/raw/public_reference_collection/acquisitions/study_usage_fulltexts_2026-11-04'


def read(path):
    return json.loads(Path(path).read_text())


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def size(value):
    if value is None:
        return 'not held / unidentified'
    if value < 1024:
        return f'{value:,} B'
    for divisor, unit in [(1024**3, 'GiB'), (1024**2, 'MiB'), (1024, 'KiB')]:
        if value >= divisor:
            return f'{value / divisor:.2f} {unit}'


def md(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def link(label, path):
    return f'[{label}](<{os.path.relpath(path, DOC.parent)}>)'


def judgement(dataset, policy):
    result = {**policy['default'], **policy['category_relevance'].get(dataset['category'], {}),
              **policy['relevance_overrides'].get(dataset['id'], {})}
    for group in policy['priority_groups']:
        if dataset['id'] in group['ids']:
            result.update(action_priority=group['priority'], action=group['action'])
            break
    else:
        if dataset['optimization_status'] == 'ready_scoped':
            result.update(action_priority='Maintain', action='Use only the existing cleared source subset; no new acquisition priority. Preserve independent evaluation boundaries.')
    if dataset['id'] in policy['cll_direct']:
        result['cll_fit'] = 'CLL disease source; exact blood-lab/temporal/clinical-validation coverage still depends on delivered fields'
    elif dataset['id'] in policy['cll_indirect_laboratory']:
        result['cll_fit'] = 'Indirect laboratory/unit support; not an established CLL-TIM validation cohort'
    else:
        result['cll_fit'] = 'No direct CLL-TIM cohort relevance established; may support generic harmonization'
    return result


def profiles(dataset):
    rows = []
    for artifact in dataset['obtained_artifacts']:
        profile = artifact['structure']
        if 'rows' in profile or 'data_lines' in profile or 'lines' in profile:
            rows.append({'path': artifact['path'], 'member': None,
                         'records': profile.get('rows', profile.get('data_lines', profile.get('lines'))),
                         'columns': profile.get('columns', profile.get('declared_attributes')),
                         'unit': 'ARFF data lines' if 'data_lines' in profile else 'NDJSON objects' if 'lines' in profile else 'table rows',
                         'status': profile['state']})
        for table in profile.get('contained_tables', []):
            # A GDC study card must not claim all ten archive members as its own.
            if dataset['id'].startswith('GDC_') and dataset['id'] != 'GDC_SM':
                token = dataset['id'][4:].lower()
                if token == 'gillette':
                    token = 'gilette'
                if '/' + token + '/' not in table['member'].lower():
                    continue
            rows.append({'path': artifact['path'], 'member': table['member'],
                         'records': table.get('rows'), 'columns': table.get('columns'),
                         'unit': 'archive-member table rows', 'status': table['state']})
    return rows


def population(dataset_id, policy, artifacts):
    check = policy['person_key_checks'].get(dataset_id)
    if not check:
        return None
    path = ROOT / check['path']
    if check['path'] not in artifacts:
        raise ValueError('population_count_source_not_in_verified_snapshot')
    keys = {key: set() for key in check['keys']}
    missing = Counter()
    count = 0
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not set(keys) <= set(reader.fieldnames or []):
            raise ValueError('native_person_key_absent')
        for row in reader:
            count += 1
            for key in keys:
                if row[key]:
                    keys[key].add(row[key])
                else:
                    missing[key] += 1
    return {'path': check['path'], 'sha256': artifacts[check['path']]['sha256'], 'rows': count,
            'keys': [{'field': k, 'meaning': check['keys'][k], 'distinct_nonempty': len(v),
                      'missing': missing[k]} for k, v in keys.items()],
            'limits': 'Exact native-key counts only; does not establish biological identity, population eligibility or independent cohorts.'}


def dataset_link(d):
    return f"[{md(d['id'] + ' — ' + d['name'])}](#{d['id'].lower()})"


def display_time(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo('Europe/Copenhagen')).strftime('%Y-%m-%d %H:%M %Z')


def availability(d):
    if not d['associated_download_files']:
        return 'not acquired'
    if d['category'] in {'metadata_only', 'standards_and_methods'} or d['completeness'] in {
            'not_obtained', 'metadata_only_or_not_obtained', 'metadata_only_patient_derivative_unacquired'}:
        return 'metadata / code / reference only'
    return 'partial package' if d['completeness'].startswith('partial') else 'selected subset held'


def access_queue(d):
    route=d['access_route']
    if 'registration' in route and 'no_registration' not in route:
        return 'registration'
    if any(t in route for t in ['author','owner','commercial','institutional']):
        return 'owner'
    held=d.get('associated_download_files',len(d.get('obtained_artifacts',[])))
    if not held and route.startswith('public'):
        return 'public_review'
    return 'local_or_hold'


def preaccess_license_review(d, policy):
    pre=policy.get('preaccess_license_policy',{})
    for review in pre.get('reviews',[]):
        if d['id'] in review['dataset_ids']:
            return review
    coverage=pre.get('coverage',{})
    if d['id'] in coverage.get('deferred_registration_ids',[]):
        return {'id':'deferred_registration','status':'deferred_not_current_download',
                'summary':coverage['deferred_reason'],
                'next':'No registration or transfer planned; review exact current terms before promotion.',
                'rights_granted':False}
    if access_queue(d)=='local_or_hold' and d.get('optimization_status')=='ready_scoped':
        return {'id':'existing_scope','status':'no_new_access_planned',
                'summary':'Existing eligible subset only; any new release needs a fresh source-term screen.',
                'next':'Use the current source-bound runtime gate; do not extend it to another version.',
                'rights_granted':False}
    return {'id':'not_reviewed','status':pre.get('default_status','not_reviewed_before_new_access'),
            'summary':'Exact pre-access terms not reviewed; request the license/agreement before applying or transferring data.',
            'next':pre.get('default_action','Review public terms or request a sample agreement first.'),
            'rights_granted':False}


def preaccess_coverage(records, policy):
    """Require a recorded outcome, without turning a review into an access grant."""
    pre=policy.get('preaccess_license_policy',{})
    coverage=pre.get('coverage')
    if not coverage:return None
    required={d['id'] for d in records if access_queue(d)=='registration'
              and d['action_priority'] in {'P1','P2'}}
    deferred={d['id'] for d in records if access_queue(d)=='registration'
              and d['action_priority']=='P3 / hold'}
    if (required!=set(coverage['required_registration_ids']) or
            deferred!=set(coverage['deferred_registration_ids'])):
        raise ValueError('preaccess_registration_census_changed_review_required')
    reviews={i:r for r in pre['reviews'] for i in r['dataset_ids']}
    if any(i not in reviews or reviews[i].get('rights_granted') is not False
           or not reviews[i].get('sources') or not reviews[i].get('evidence') for i in required):
        raise ValueError('preaccess_registration_outcome_or_evidence_missing')
    with_agreement=sum(reviews[i].get('public_dataset_or_route_agreement_reviewed') is True for i in required)
    return {'current_registration_candidates':len(required),'documented_outcomes':len(required),
            'public_dataset_or_route_agreement_reviewed':with_agreement,
            'complete_current_package_terms_still_needed':len(required)-with_agreement,
            'deferred_registration_entries':len(deferred),
            'statuses':dict(Counter(reviews[i]['status'] for i in sorted(required))),
            'rights_granted':0}


def render_catalogue(report, usage, policy, validation, folder):
    records = report['datasets']
    by_id = {d['id']: d for d in records}
    checked = validation['summary']
    lit_path = ROOT / 'configs/harmonization_literature_review.json'  # project-specific literature review; optional
    lit = read(lit_path) if lit_path.exists() else {'search_window': 'none recorded', 'indexed_records_screened': 0, 'reviewed_studies': [], 'citation_trace_summary': [], 'queries': []}
    fulltexts = {f['study_id']: f for f in read(FULLTEXTS / 'fulltext_report.json')['studies']} if (FULLTEXTS / 'fulltext_report.json').exists() else {}  # optional in a fresh workspace
    ft_counts = Counter(f['status'] for f in fulltexts.values())
    lines = ['# Datasets: availability, selection and benchmark readiness', '',
        '**Browse the [HTML dataset overview](dataset-browser/index.html)** for filters and linked dataset pages. '
        'The maintained Markdown sources are this catalogue and the [action/access plan](DATASET_ACTION_PRIORITIES.md). '
        'This file answers what is held, how complete/useful it is, which tools used it, and what prevents a benchmark. '
        'The plan answers what to do next and where registration or owner contact is required.', '',
        f"Inventory checked {display_time(report['generated_at'])}; deterministic format/permission snapshot {display_time(validation['checked_at'])}. "
        f"**{len(records)} entries** include shared releases, metadata, standards and pending cohorts—not independent patient populations.", '',
        '## Current position', '',
        '| Question | Answer |', '|---|---|',
        f"| Download identities | {checked['locked_artifacts']} locked artifacts; {md(checked['locked_integrity'])} |",
        f"| Prepared original identities | {checked['prepared_artifacts']} files; {md(checked['prepared_integrity'])} |",
        '| Format-check limits | Hashes cover retained identities; native/binary formats, bounded archive/NDJSON reads, missing-cell meaning and clinical semantics are not all certified. Per-artifact results remain in the validation receipt. |',
        f"| Scoped development/regression inputs | {checked['optimization'].get('ready_scoped',0)} prepared subsets pass identity, full rectangular reading and lenient local inference/embedding preflight |",
        '| Independent clinical benchmark ready | **None certified by this audit.** Source/reference review, a frozen held-out contract and applicable target/component rights remain required. |',
        f"| Needs preparation/permissions | {checked['optimization'].get('needs_preparation_or_permissions',0)} entries |",
        f"| Incomplete | {checked['optimization'].get('incomplete',0)} entries; this can mean missing gold/dictionary rather than missing patient rows |",
        f"| Reference-only / excluded | {checked['optimization'].get('reference_only',0)} / {checked['optimization'].get('not_appropriate',0)} |", '',
        '### Ready for scoped development or regression', '',
        'These are exact source subsets, not blanket approval of their parent archives. Ready does not certify clinical correctness, absolute-time extraction or independent generalization.', '',
        '| Dataset | Source-only package | Files | Purpose / limit |', '|---|---|---:|---|']
    for d in records:
        for s in d['input_scopes']:
            if s['status']=='ready_scoped':
                lines.append(f"| [{d['id']}](#{d['id'].lower()}) | {s['id']} | {s['file_count']} | {md(s['task'])} |")
    lines += ['', '### Most useful benchmark candidates still requiring work', '',
        '| Reference family | Held / missing | Blocking action |', '|---|---|---|',
        '| FCN FHS/MESA/ARIC | 885 labeled metadata descriptions / 64 concepts held; no patients needed | Blinded DOCX adapter, rights/label review and cohort/concept-aware split |',
        '| GDC-SM / Magneto / MetaHarmonizer | Ten prepared cancer tables and target/annotation versions held; original XLSX 0/10 | Choose reference version; review invalid labels, study-aware scoring and source rights |',
        '| NIH CDEMapper | Four dictionaries, 494 rows / 264 mapped; 22,743-candidate snapshot held | Resolve 12 absent IDs, candidate version, multi-target/negative scoring and terms |',
        '| HF CDE-Mapper | Paper/code evidence held; 476-CDE TIME-CHF/CHECK-HF references missing | Author metadata/reference request |',
        '| ADHTEB / AI-READI / CDM-to-FHIR | Public dictionaries/mappings or supplement held | Hide answers/prior predictions, review damaged/incomplete/custom targets and component terms |',
        '| Scottish labs / UCUM / ATHLOS | Public rules or external expected-outcome cases held | Appropriate rule/identifier/context review; component checks are not patient-event gold |', '',
        '### Reading the lists', '',
        '- **Availability** distinguishes a selected subset, partial package, metadata/code-only holding and no identified payload. Download completion does not mean benchmark completeness.',
        '- **Size** is retained locked-file size, often compressed and including notices/code. Shared archives are flagged; never add shared entries into a disk or cohort total. Prepared copies are reported separately. Full provider-release sizes remain unknown unless an advertised artifact is specifically identified.',
        '- **Counts** distinguish table rows, labels, NDJSON objects and native person/stay keys. No arbitrary completeness percentage or pooled participant total is invented.',
        '- **Relevance** concerns generic clinical harmonization; CLL fit is separate. Unassessed means insufficient source-specific evidence. P1/P2/P3 are action priorities, not quality scores or download approval.',
        '- **Permissions:** lenient is the default AI criterion; underlying rights, versioned evidence, privacy/provider conditions and operation-specific gates still apply. A blocked/unregistered package is not proof of an explicit AI ban. The mandatory [license policy](DATA_LICENSES_AND_AI_USE.md) contains both modes and exact evidence.',
        '- Public sources may be in model pretraining; a project-held-out split does not prove pretraining independence. Keep source/gold, previous model answers, target-derived aliases and overlapping cohorts separate.', '',
        '## All datasets', '',
        'Use the ID links for compact details, formats, missing components, source citations, local paths and overlap families.', '',
        '| Dataset | Available | Retained size | Relevance | Priority | Development |', '|---|---|---|---|---|---|']
    for d in records:
        lines.append('| '+' | '.join([dataset_link(d),availability(d),size(d['associated_download_bytes'])+(' shared' if d['shared_downloads'] else ''),d['workflow_relevance'],d['action_priority'],d['optimization_status']])+' |')
    lines += ['', '## Which tools used which datasets', '',
        'This records published source/application relationships, not new local tool runs. Full cohorts, demos, author-prepared subsets and software examples are distinct. GenHPF/TransTab are predictive/representation studies included for traceability, not direct semantic-mapping accuracy baselines. Exact scope/version limits are stated in each dataset entry.', '',
        '| Study / tool | Registered dataset relationships |', '|---|---|']
    for ident in policy['benchmark_tool_studies']:
        study = next((s for s in usage['studies'] if s['id']==ident),None)
        if not study:continue
        uses=[]
        for d in records:
            for u in d['uses']:
                if u['study']==ident:
                    uses.append(f"[{d['id']}](#{d['id'].lower()}) ({md(u['relation'])})")
        lines.append(f"| [{md(study['title'])}](#study-{ident}) | "+('; '.join(uses) or 'No exact local dataset application established; inspect paper and source access statement')+' |')
    lines += ['', '### Comparator role and software holdings', '', '| Tool | Role | Status and dataset boundary |', '|---|---|---|']
    for tool in policy['tool_roles']:
        lines.append(f"| [{tool['name']}]({tool['source']}) | {md(tool['role'])} | {md(tool['status']+' '+tool['dataset'])} |")
    lines += ['', '## Dataset details', '', 'Expand an entry for the selection evidence. File-by-file hashes/profiles remain in local machine-readable manifests rather than duplicated long tables.', '']
    for d in records:
        lines += [f'<a id="{d["id"].lower()}"></a>',f"### {d['id']} — {d['name']}", '', '<details>', '<summary>Scope, size, readiness, references, problems and sources</summary>', '',
            '**Represents:** '+d['scope'], '',
            f"**Selection:** {d['workflow_relevance']} relevance; {d['action_priority']}. {d['reason']} **CLL fit:** {d['cll_fit']}.", '',
            f"**Holding:** {availability(d)}; {size(d['associated_download_bytes'])} in {d['associated_download_files']} locked files"+(' (shared containers)' if d['shared_downloads'] else '')+f". Prepared representations: {size(d['prepared_artifact_bytes'])} / {d['prepared_artifact_files']} files. Full provider corpus size: unknown.", '',
            f"**Completeness / readiness:** {d['completeness']}; {d['integrity_status']}; development {d['optimization_status']}; independent benchmark {d['benchmark_status']}.", '',
            '**Input/output/reference:** '+d['reference_contract'], '']
        if d['native_key_counts']:
            c=d['native_key_counts']
            lines += [f"**Observed native keys:** {c['rows']:,} rows in {Path(c['path']).name}; "+'; '.join(f"{k['field']}: {k['distinct_nonempty']:,} distinct nonempty ({k['meaning']}), {k['missing']:,} missing" for k in c['keys'])+'. Counts apply to that source file only.', '']
        elif d['observed_table_profiles']:
            p=d['observed_table_profiles'][0]
            lines += [f"**Observed structure:** {p['records'] if p['records'] is not None else 'unknown'} {p['unit']}; {p['columns'] if p['columns'] is not None else 'unknown'} fields in {p['member'] or Path(p['path']).name}. Additional table/representation profiles: {max(0,len(d['observed_table_profiles'])-1)}; full inventory below.", '']
        else:
            lines += ['**Observed row/participant count:** not established for a workflow input; published metadata counts in the scope are not participant counts.', '']
        if d['known_pending_artifact']:
            k=d['known_pending_artifact'];lines += [f"**Known pending artifact:** {k['bytes']:,} bytes ({size(k['bytes'])}), {k['kind']}; "+link('provider metadata',ROOT/k['evidence'])+'.','']
        lines += ['**Problems / gaps:** '+(d['review_note'] or 'Reference correctness and task-specific case independence are not certified.')+' '+ ' '.join(d['missing_components']), '']
        for note in policy.get('curation_notes',{}).get(d['id'],[]):lines += ['- '+note]
        lines += ['', '**Next action:** '+d['action']+' '+d['next_action'], '',
            '**Access:** '+d['access_route']+(' — [register/request]('+d['registration_url']+')' if d.get('registration_url') else '')+'.', '',
            '**Licensing before new access:** '+d['preaccess_screen']['status']+'. '+d['preaccess_screen']['summary']+' This preliminary screen grants no data-access or AI-use rights; see the [shared terms review](DATASET_ACTION_PRIORITIES.md#license-checks-before-registration-or-download).', '',
            '**AI use:** associated-package local inference/embedding gate '+d['associated_download_permissions']['status']+' (lenient snapshot). '+('; '.join(s['id']+': '+s['status'] for s in d['input_scopes']) or 'No prepared, permission-checked source-only scope')+'. Candidate vocabularies, remote operations, training and redistribution require their own runtime checks.', '',
            '**Where / provenance:** '+link('local dataset folder',ROOT/d['workspace']/'README.md')+' · '+link('exact manifest',ROOT/d['workspace']/'manifest.json')+'. Overlap families: '+', '.join(d['leakage_families'])+'.', '',
            '**Sources:** '+' · '.join('[provider '+str(i+1)+']('+u+')' for i,u in enumerate(d['source_urls']))+'.', '',
            '**Studies / benchmark use:**']
        for u in d['uses']:
            lines += [f"- [Study {u['study']}](#study-{u['study']}) — **{u['relation']}**. {u['scope']}"]
        if not d['uses']:lines += ['- No independently verified paper application of the exact artifact established; provider evidence only.']
        diagram=policy.get('structure_diagrams',{}).get(d['id'])
        if diagram:lines += ['', '**Structure of the documented prepared subset** (not a claim about an unacquired full corpus):', '', '```mermaid',diagram,'```']
        lines += ['', '</details>', '']
    lines += ['## Studies, citations and full texts', '',
        f"{len(usage['studies'])} tracked studies/projects; {ft_counts['downloaded_verified']} retrieved article copies, {ft_counts['already_held_verified']} reused, {ft_counts['not_retrieved']} not retrieved. Alternate XML/PDF copies are not new studies. Source publications, downstream applications and software/provider evidence are different relationships.", '',
        f"Literature coverage: dated {lit['search_window']}; {lit['indexed_records_screened']} indexed records screened; {len(lit['reviewed_studies'])} additional study reviews. Review depths: {md(dict(Counter(s['review_depth'] for s in lit['reviewed_studies'])))}. This bounded search is not an exhaustive census or a replication of every cited benchmark.", '']
    for study in usage['studies']:
        ident=study['id'];ft=fulltexts.get(ident,{'status':'not_registered'})
        lines += [f'<a id="study-{ident}"></a>',f"**{study['title']}** ({study.get('document_type','study/project')}). "+' · '.join('[source '+str(i+1)+']('+u+')' for i,u in enumerate(study.get('urls',[])))+'.']
        if ft['status'] in {'already_held_verified','downloaded_verified'}:
            path=ROOT/ft['path'] if ft.get('path_relative_to')=='repository' else FULLTEXTS/ft['path']
            if not path.is_file() or file_digest(path)!=ft['sha256']:raise ValueError('fulltext_identity_changed:'+ident)
            lines += [link('Verified local fulltext',path)+'; '+ft['status']+'.']
        else:lines += ['Fulltext: '+ft['status']+'. Publication link/abstract is not a downloaded full article.']
        related=[f"[{d['id']}](#{d['id'].lower()})" for d in records if any(u['study']==ident for u in d['uses'])]
        lines += ['Related entries: '+(', '.join(related) or 'context/method reference; no exact local dataset use established')+'.','']
    lines += ['## Validation, provenance and maintenance', '',
        'Provider originals and previously sealed input/run versions remain untouched. Preparation records decompression, native-to-CSV projections, source types/NA masks and explicit minor versioned fixes. Relative-time anchors, missing dates, ambiguous QC legends and invalid reference labels are not silently repaired.', '',
        'This catalogue was built without inference, embeddings or new downloads. Deterministic identity/format checks are separate from clinical mapping validity. No independent benchmark readiness or model success is inferred from downloading or parsing a file.', '',
        '- '+link('Current validation receipt',ROOT/'data/interim/dataset_validation/latest.json'),
        '- '+link('Current machine-readable selection',folder/'selection.json'),
        '- '+link('Acquisition/preparation/run ledger',ROOT/'runs/provenance/summary.json'),
        '- '+link('Documentation consolidation recovery manifest',ROOT/'configs/documentation_layout.json'), '',
        'Rebuild current evidence and this single catalogue:', '', '```sh',
        '.venv/bin/python Scripts/audit_dataset_downloads.py',
        '.venv/bin/python Scripts/validate_dataset_workspace.py',
        '.venv/bin/python Scripts/document_dataset_selection.py',
        '.venv/bin/python Scripts/build_data_provenance.py', '```', '',
        'The catalogue command also regenerates the [offline HTML browser](dataset-browser/index.html), including per-dataset pages, studies, actions and licence policy. Rebuild only the HTML with `.venv/bin/python Scripts/build_dataset_browser.py` when the current evidence snapshot is unchanged.', '',
        'Acquisition commands, MCP metadata, minimal-request instructions and future input-version rules are in the [action/access plan](DATASET_ACTION_PRIORITIES.md). Machine-readable policy/evidence is under `configs/` and ignored `data/`; historical analytical reports are not alternative dataset catalogues.', '']
    DOC.write_text('\n'.join(lines))


def update_action_queues(records, validation, policy=None):
    path=ROOT/'docs/DATASET_ACTION_PRIORITIES.md'
    text=path.read_text()
    start,end='<!-- current-access-queues:start -->','<!-- current-access-queues:end -->'
    text=text.split(start,1)[0] if start in text else text
    if 'No datasets are registered yet' in text:  # the empty-workspace placeholder gives way to the generated queues
        text='# Dataset actions: priorities, requests and completion gates\n'
    text=text.rstrip()+'\n'
    pre=(policy or {}).get('preaccess_license_policy',{})
    lines=[start,'## License checks before registration or download','',
        'Check publicly available licences, DUA templates, dataset-use limitations, research scope, software/model deployment, export and publication terms **before spending time on an account/application or a dataset transfer**. If a necessary term is only available from the owner or after login, record that gap and ask for a sample agreement or scope clarification first. This is an acquisition-planning stage; the downloader review and the source-bound AI gate remain separate.', '',
        'Pre-access statuses: **public_terms_screened_conditional** = a public dataset/route agreement reviewed, with project/environment conditions remaining; **public_notice_only_agreement_pending** = notice only; **public_route_checked_terms_unavailable** = access route checked but full/current terms unavailable; **platform_terms_only_package_pending** = platform reviewed, dataset grant missing; **component_terms_and_author_grant_pending** = terminology/label grants unresolved; **owner_terms_required** = obtain actual package terms; **deferred_not_current_download** = P3, outside this public review; **not_reviewed_before_new_access** = not screened. None authorizes data transfer or AI use. Lenient mode still blocks the AI criterion only for an explicit applicable prohibition; underlying rights and non-AI conditions must also be satisfied.', '']
    coverage=preaccess_coverage(records,policy or {})
    if coverage:
        lines += [f"**Coverage checked {pre['reviewed_on']}: {coverage['documented_outcomes']}/{coverage['current_registration_candidates']} current P1/P2 registration/access candidates have a recorded public-review outcome.** {coverage['public_dataset_or_route_agreement_reviewed']} have a public dataset/route agreement reviewed; {coverage['complete_current_package_terms_still_needed']} still need full/current/package-specific terms. This does not mean any final project grant is complete. The {coverage['deferred_registration_entries']} P3 registration entries are explicitly deferred and not claimed as screened. P2 entries are conditional alternatives, not an instruction to register for all of them.", '',
            'Important conditions: ELSA/UKDS requires written provider permission for internet/cloud tools, including online AI; that is not a blanket ban on standalone offline models. AD Workbench restricts external downloads except approved outputs. EHRSHOT has derived-model/dataset licensing obligations. BioLINCC uses project/user-specific terms and retention limits. HiRID requires contributor review. See the source-specific rows below.', '']
    lines += [
        '| Sources | Preliminary licensing status | Before registering or requesting the package | Public evidence |',
        '|---|---|---|---|']
    for review in pre.get('reviews',[]):
        source_links=' · '.join('[source '+str(i+1)+']('+u+')' for i,u in enumerate(review.get('sources',[]))) or 'No dataset-specific grant in the project register; obtain owner terms'
        lines.append('| '+', '.join(review['dataset_ids'])+' | '+md(review['status']+': '+review['summary'])+' | '+md(review['next'])+' | '+source_links+' |')
    for review in pre.get('reviews',[]):
        if review.get('component_reviews'):
            lines += ['', '**'+', '.join(review['dataset_ids'])+' component review:**','']
            lines += ['- '+c['cohort']+': '+c['outcome']+' [source]('+c['source']+').' for c in review['component_reviews']]
    lines += ['', 'The partner/source agreement must match the actual trial, release and environment. A preliminary public-template review does not establish that an institution signed it or that the final delivery has identical terms. A notice with no disease/use limitations is not a public-domain or unrestricted-AI grant. Missing terms are not silently interpreted as permission or an AI ban.', '',
        'Order: public terms → minimal schema/reference specification → resolve feasibility/conditions → account and project request if worthwhile → applicable accepted/executed terms and approved delivery → source identity and operation-specific AI preflight.', '',
        'Questions to resolve before an application:', '']
    lines += ['- '+q for q in pre.get('questions',[])]
    lines += ['', '## Complete access and action queues','',
        'These queues are generated from the same snapshot as the catalogue. They describe required routes, not submitted applications, approved downloads or permission to run AI. Full eICU/FHIR access remains separate from held demos. See each catalogue entry for minimal requested fields and missing components.','']
    queues=[('Registration / institution / provider approval',lambda d:access_queue(d)=='registration'),
            ('Author / owner request or other permissioned access',lambda d:access_queue(d)=='owner'),
            ('Public leads not acquired: metadata review before download',lambda d:access_queue(d)=='public_review')]
    for title,predicate in queues:
        chosen=[d for d in records if predicate(d)]
        lines += ['### '+title,'',f'{len(chosen)} registry entries; related trials/families can be alternatives rather than independent requests.','',
            '| Priority | Dataset | Licensing first | Required action | Source / request |','|---|---|---|---|---|']
        rank={'P1':0,'P2':1,'Maintain':2,'P3 / hold':3}
        for d in sorted(chosen,key=lambda x:(rank[x['action_priority']],x['id'])):
            url=d.get('registration_url') or d['source_urls'][0]
            screening=d.get('preaccess_screen') or preaccess_license_review(d,policy or {})
            lines.append(f"| {d['action_priority']} | [{md(d['id']+' — '+d['name'])}](DATASET_SELECTION.md#{d['id'].lower()}) | {md(screening['status'])} | {md(d['next_action'])} | [route]({url}) |")
        lines += ['']
    lines += ['## Acquisition, MCP and file handling','',
        '- Start with the catalogue and choose the exact minimal package. Inspect dictionaries, archive members, source/target roles, overlap, sizes, terms and intended task before adding a transfer. Priority does not approve downloading.','',
        '- Active reviewed public jobs: `configs/research_resources.json`; exact artifact approvals: `configs/dataset_download_reviews.json`; content scope: `configs/resource_scope.json`. No broad image, raw microarray, generic ML or redundant population expansion.','',
        '- GitHub MCP is used to discover/pin commit IDs, recursive trees, Git blob identities and selected paths. Versioned metadata is under `configs/mcp/`; it is not a standalone running MCP server. The downloader fetches original bytes over HTTPS and verifies pins/checksums. Replaying pinned downloads does not need an MCP session. Connector credentials are never stored here.','',
        '```sh',
        '# Reviewed public artifacts only; --id can restrict to an approved artifact.',
        '.venv/bin/python Scripts/acquire_research_resources.py --execute --workers 6',
        '.venv/bin/python Scripts/audit_dataset_downloads.py',
        '.venv/bin/python Scripts/review_pending_resources.py',
        '# Articles have a separate metadata/download/verify queue.',
        '.venv/bin/python Scripts/acquire_study_fulltexts.py verify',
        '```','',
        '- Starter/provider bytes remain under `data/raw/public_reference_collection/downloaded_data/` and dated `acquisitions/`. Source-only views, targets/answers, documentation and type/NA masks are separate under `prepared/`. Dataset folders are navigation/manifest records, not bulk workflow input roots.','',
        '- Approved manual deliveries go to `data/incoming_manual/<dataset>/<release>/`, with provider file list, dictionary, release/URL, terms and checksums. Keep credentials and signed/private agreement details outside Git. Verify the delivery against the requested minimum before preparing inputs.','',
        '- Preserve original bytes. New adapters/minor fixes belong in versioned input folders with parent/source hashes, precise transformations and retained original values. No invented time anchors, silent label repair, float-converted identifiers or answer-bearing columns in discovery inputs.','',
        '- Run the source-bound licence preflight in lenient mode by default before inference/embeddings; include target vocabularies and metadata. Remote inference, training and redistribution need separate decisions. Restrictive mode still requires explicit AI approval. See [mandatory operational policy](DATA_LICENSES_AND_AI_USE.md).','',
        '- Future live quality comparisons test Gemma, GPT-OSS and a third installed model, with fixed independent validation/unsafe-acceptance gates and per-model protocol/recovery results. No benchmark tool or model was executed by this documentation cleanup.','',
        '## Documentation maintenance','',
        'Only `DATASET_SELECTION.md` and this file are maintained dataset-facing documents. The mandatory licence policy, source registries, locks, machine reports and sealed analytical runs remain technical evidence. Superseded catalogues/atlas pages/report generators were removed from active paths; their before-bytes are retained in the local checksum-bound recovery snapshot.','',
        'Update curated selection/relevance and grouped priorities in `configs/dataset_selection.json`, provider/study facts in `configs/dataset_study_usage.json`, and source/permission evidence through their existing gated tools. Re-run validation and `Scripts/document_dataset_selection.py`; do not create another parallel catalogue.',end,'']
    path.write_text(text.rstrip()+'\n\n'+'\n'.join(lines))

def main():
    policy, usage = read(POLICY), read(USAGE)
    receipt_path = ROOT / 'data/interim/dataset_validation/latest.json'
    receipt = read(receipt_path)
    source = ROOT / receipt['report']
    if file_digest(source) != receipt['report_sha256']:
        raise ValueError('validation_report_changed')
    validation = read(source)
    ids = {d['id'] for d in usage['datasets']}
    if ids != {d['id'] for d in validation['datasets']}:
        raise ValueError('registry_and_validation_dataset_census_differ')
    named = [i for group in policy['priority_groups'] for i in group['ids']]
    if len(named) != len(set(named)) or set(named) - ids or set(policy['relevance_overrides']) - ids:
        raise ValueError('invalid_selection_policy_ids')
    preaccess=policy.get('preaccess_license_policy',{}).get('reviews',[])
    screened=[i for review in preaccess for i in review['dataset_ids']]
    if (len(screened)!=len(set(screened)) or set(screened)-ids or
            any(review.get('rights_granted') is not False for review in preaccess)):
        raise ValueError('invalid_preaccess_screen_or_false_access_grant')
    # Recheck identities, not just cached sizes, before publishing selection facts.
    artifacts = {f['path']: f for f in validation['artifacts']}
    for f in artifacts.values():
        p = ROOT / f['path']
        if not p.is_file() or p.stat().st_size != f['bytes'] or file_digest(p) != f['sha256']:
            raise ValueError('selection_source_missing_or_changed: ' + f['path'])
    supplement_path = ROOT / 'data/raw/public_reference_collection/acquisitions/harmonization_review_2026-11-04/recent_reference_inspection.json'
    supplements = read(supplement_path) if supplement_path.exists() else {'files': []}  # optional inspection record
    supplement_profiles = {}
    for artifact in supplements['files']:
        path = artifact['path']
        if path not in artifacts or artifact['sha256'] != artifacts[path]['sha256']:
            raise ValueError('supplement_profile_identity_mismatch')
        tables = []
        for index, table in enumerate(artifact.get('word_tables', []), 1):
            is_fcn_reference = Path(path).name == 'fcn_metadata_appendix.docx' and index == 1
            tables.append({'path': path, 'member': f'Word table {index}',
                           'records': table['rows'] - 1, 'columns': ', '.join(table['column_widths']),
                           'unit': 'annotated metadata records' if is_fcn_reference else 'result/method table rows excluding first row; merged headers may remain',
                           'status': 'hash_bound_specific_document_inspection'})
        for table in artifact.get('xlsx_tables', []):
            tables.append({'path': path, 'member': table['member'] + ' → ' + table['sheet'],
                           'records': table['nonempty_rows_including_header'], 'columns': len(table['header']),
                           'unit': 'nonempty worksheet rows INCLUDING header; gold/model-result roles require adapter',
                           'status': 'hash_bound_specific_document_inspection'})
        if tables:
            supplement_profiles[path] = tables
    associated = Counter(f['path'] for d in validation['datasets'] for f in d['obtained_artifacts'])
    fulltexts = {f['study_id']: f for f in read(FULLTEXTS / 'fulltext_report.json')['studies']} if (FULLTEXTS / 'fulltext_report.json').exists() else {}  # optional in a fresh workspace
    studies = {s['id']: s for s in usage['studies']}
    generation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = BASE / generation
    records = []
    for d in validation['datasets']:
        d={**d,'name':d['name'].replace(' — first priority','')}
        decision = judgement(d, policy)
        raw = {f['path']: f for f in d['obtained_artifacts']}
        prepared = {p: artifacts[p] for scope in d['input_scopes'] for p in scope['files']}
        # Also describe existing prepared representations; these are not all ready.
        resources = set(policy['prepared_resource_groups'].get(d['id'], []))
        prepared.update({p: f for p, f in artifacts.items() if f.get('resource') in resources})
        count = population(d['id'], policy, artifacts)
        record = {**d, **decision,
                  'preaccess_screen':preaccess_license_review(d,policy),
                  'associated_download_bytes': sum(f['bytes'] for f in raw.values()) if raw else None,
                  'associated_download_files': len(raw),
                  'shared_downloads': [p for p in raw if associated[p] > 1],
                  'prepared_artifact_bytes': sum(f['bytes'] for f in prepared.values()) if prepared else None,
                  'prepared_artifact_files': len(prepared),
                  'provider_full_corpus_bytes': None,
                  'known_pending_artifact': policy['known_pending_artifact_bytes'].get(d['id']),
                  'observed_table_profiles': profiles(d) + [p for path in raw for p in supplement_profiles.get(path, [])], 'native_key_counts': count,
                  'prepared_artifact_profiles': [{'path': p, 'bytes': f['bytes'], 'role': f.get('prepared_role'),
                                                  'profile': f['structure']} for p, f in prepared.items()],
                  'reference_contract': policy['reference_contracts'].get(d['id'],
                     'See held scope and missing components. No independently validated source-to-output contract is certified by this inventory; original files may mix inputs, references, code and prior model results.'),
                  'study_details': [{**studies[u['study']], 'application': u,
                                     'fulltext': fulltexts.get(u['study'], {'status': 'not_registered'})} for u in d['uses']]}
        records.append(record)
    summary = {'entries': len(records), 'priority': dict(Counter(d['action_priority'] for d in records)),
               'workflow_relevance': dict(Counter(d['workflow_relevance'] for d in records)),
               'validation_id': validation['validation_id'], 'rechecked_unique_artifacts': len(artifacts),
               'source_repairs': 0, 'downloads': 0, 'model_calls': 0}
    summary['preaccess_coverage']=preaccess_coverage(records,policy)
    evidence_paths = [p for p in [POLICY, USAGE, source, receipt_path, supplement_path, Path(__file__),
                      ROOT / 'docs/DATASET_ACTION_PRIORITIES.md', FULLTEXTS / 'fulltext_report.json'] if p.exists()]  # optional records may be absent in a fresh workspace
    for review in policy.get('preaccess_license_policy',{}).get('reviews',[]):
        for item in review.get('evidence',[]):
            path=ROOT/item['path']
            if not path.is_file() or file_digest(path)!=item['sha256']:
                raise ValueError('preaccess_license_evidence_missing_or_changed:'+item['path'])
            if path not in evidence_paths:evidence_paths.append(path)
    evidence = [{**snapshot_path(p, ROOT), 'sha256': file_digest(p)} for p in evidence_paths]
    for p, item in zip(evidence_paths, evidence):
        copy = folder / 'code_and_evidence' / snapshot_copy_path(item)
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, copy)
    report = {'schema_version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
              'summary': summary, 'evidence': evidence, 'datasets': records}
    write(folder / 'selection.json', report)
    update_action_queues(records, validation, policy)
    # Capture the generated action queues as part of the evidence snapshot.
    action_path=ROOT/'docs/DATASET_ACTION_PRIORITIES.md'
    for e in evidence:
        if e['path']=='docs/DATASET_ACTION_PRIORITIES.md':e['sha256']=file_digest(action_path)
    shutil.copyfile(action_path,folder/'code_and_evidence/docs/DATASET_ACTION_PRIORITIES.md')
    write(folder/'selection.json',report)
    render_catalogue(report, usage, policy, validation, folder)
    write(BASE / 'latest.json', {'selection': str((folder / 'selection.json').relative_to(ROOT)),
                               'selection_sha256': file_digest(folder / 'selection.json'),
                               'document': str(DOC.relative_to(ROOT)), 'document_sha256': file_digest(DOC), 'summary': summary})
    from build_dataset_browser import build
    build()
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
