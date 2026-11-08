"""Presentation facets derived from the existing, source-bound inventory only."""
from pathlib import Path

FACETS={
 'project_relevance':('Relevance annotation for this research study',{'high':'High','moderate':'Moderate','low':'Low','unassessed':'Not yet annotated'}),
 'project_role':('Intended role in this research study',{'development':'Development / optimization','benchmark':'Benchmark candidate','reference':'Reference / context','exclude':'Exclude from this study','unassessed':'Not yet annotated'}),
 'project_tags':('Study annotation tags',{'untagged':'No study tags yet'}),
 'study_shortlist':('Study shortlist',{'shortlisted':'Saved to this study','not_shortlisted':'Not in this study shortlist'}),
 'review_route':('Licence review record',{'evidence_reviewed':'Evidence reviewed — recorded scope','manual_required':'Needs manual review / clarification','manual_recorded':'Manual decision on record'}),
 'priority':('Baseline priority (default study)',{'P1':'P1 — next actions','P2':'P2 — conditional follow-up','Maintain':'Maintain current subset','P3 / hold':'P3 — deferred / hold'}),
 'ready':('Development readiness',{'ready_scoped':'Ready — exact scoped subset','needs_preparation_or_permissions':'Needs preparation / permission review','incomplete':'Incomplete','reference_only':'Reference only','not_appropriate':'Excluded from current task'}),
 'available':('Download status',{'selected subset held':'Selected data / reference files downloaded','partial package':'Partially held / package incomplete','metadata / code / reference only':'Documentation / code / reference-only holding','not acquired':'Not downloaded'}),
 'access':('Registration / permission route',{'public_no_registration':'Public — no registration','registration':'Registration — new / full access','owner':'Owner / author request','public_review':'Public lead — review first','unresolved':'Route not yet established'}),
 'benchmark':('Published evaluation use',{'mapping':'Mapping / harmonization','extraction':'Clinical extraction','prediction':'Prediction / representations','context':'Source / related context only'}),
 'kind':('Resource category',{'clinical_sources':'Clinical source tables','mapping_references':'Dictionaries / reference mappings','synthetic_controls':'Synthetic data / software controls','tabular_controls':'Tabular prediction controls','standards_and_methods':'Standards / terminology / ETL','metadata_only':'Documentation only','pending_access':'Pending package — type unverified','excluded':'Excluded controls'}),
 'license_status':('Permission for intended local AI use',{'approved':'Approved — recorded local AI scope','source_reviewed':'Source approved — exact files pending','review_required':'Needs licence / permission review','prohibited':'Prohibited for intended local AI use'}),
 'license':('Licence family in recorded evidence',{'attribution':'CC BY / attribution','restricted_cc':'Creative Commons with NC / ND restrictions','sharealike':'ODbL / share-alike','permissive':'MIT / Apache / BSD data component','public_domain':'CC0 / public domain','custom':'Custom / terminology terms','dua':'Provider / DUA terms (screen only)','unknown':'Unknown / unresolved'}),
 'work':('What still needs work',{'preparation':'Source preparation / adapter','missing':'Missing components / reference','rights':'Rights / AI preflight unresolved','registration':'Registration for new / full access','owner':'Owner / author request','reference':'Reference / held-out review','format':'File / format problems'}),
 'relevance':('Baseline relevance (default study)',{'high':'High','moderate':'Moderate','low':'Low','unassessed':'Not yet established'}),
 'cll':('CLL relevance',{'direct':'Direct CLL source','indirect':'Indirect laboratory / unit support','other':'No direct CLL link established'}),
 'format':('Formats actually held',{'csv':'CSV / TSV / delimited','excel':'Excel','json':'JSON / NDJSON','xml':'XML / FHIR','clinical_binary':'SAS / Stata / SPSS / R native','arff':'ARFF','archive':'Archive / compressed package','document':'PDF / Word / text documentation','code':'Code / notebooks / queries','ontology':'Ontology / terminology','other':'Other / native format','not_held':'Not held'})
}
SIZE_OPTIONS={'lt1':'Under 1 MiB','1to10':'1–10 MiB','10to100':'10–100 MiB','100to1024':'100 MiB–1 GiB','gte1024':'1 GiB or more','unknown':'Unknown / not held'}
FACETS['blocker']=('Why it is not ready',{
 'prep_rights':'Local AI permission not approved','prep_reference':'Reference review / frozen split required','prep_control':'Control resource, not independent clinical gold',
 'prep_version':'Version / snapshot mismatch','prep_format':'File / format problems','prep_adapter':'Adapter or source/reference separation needed','prep_access':'Registration / approval still needed',
 'inc_access':'Registration, DUA or owner approval outstanding','inc_licence':'Licence / terms not established',
 'inc_reference':'Dictionary, labels or reference package missing','inc_records':'Participant / source records missing','inc_identity':'Exact files / version not verified','inc_other':'Other recorded gap'})
# Sub-options rendered under a parent filter option: facet key -> parent value -> (nested facet key, its values).
SUBOPTIONS={'ready':{'needs_preparation_or_permissions':('blocker',['prep_rights','prep_reference','prep_control','prep_version','prep_format','prep_adapter','prep_access']),
                     'incomplete':('blocker',['inc_access','inc_licence','inc_reference','inc_records','inc_identity','inc_other'])}}
NESTED_FACETS={key for parents in SUBOPTIONS.values() for key,_ in parents.values()}
FACETS['size_retained']=('Retained source-artifact size',SIZE_OPTIONS)
FACETS['size_prepared']=('Prepared representation size',SIZE_OPTIONS)
FACETS['size_provider']=('Verified advertised package size',SIZE_OPTIONS)


def size_band(value):
    if value is None:return 'unknown'
    for limit,label in [(2**20,'lt1'),(10*2**20,'1to10'),(100*2**20,'10to100'),(2**30,'100to1024')]:
        if value<limit:return label
    return 'gte1024'


def license_family(text):
    t=text.lower()
    if any(s in t for s in ['not located','not obtained','not supplied','unresolved','pending','grant unresolved','no umbrella']):return 'unknown'
    if any(s in t for s in ['odbl','share-alike','by-sa']):return 'sharealike'
    if any(s in t for s in ['by-nc','by-nd','by nc','by nd']):return 'restricted_cc'
    if 'cc-by' in t or 'cc by' in t:return 'attribution'
    if 'cc0' in t or 'public domain' in t:return 'public_domain'
    if any(s in t for s in ['mit','apache','bsd']):return 'permissive'
    return 'custom'


def file_format(path):
    p=path.lower()
    if p.endswith(('.csv','.tsv','.csv.gz','.tsv.gz')):return 'csv'
    if p.endswith(('.xlsx','.xls')):return 'excel'
    if p.endswith(('.json','.jsonl','.ndjson','.json.gz')):return 'json'
    if p.endswith(('.xml','.xml.gz')):return 'xml'
    if p.endswith(('.sas7bdat','.xpt','.dta','.sav','.rds','.rda','.rdata')):return 'clinical_binary'
    if p.endswith(('.arff','.arff.gz')):return 'arff'
    if p.endswith(('.zip','.tar.gz','.tgz','.gz','.xz')):return 'archive'
    if p.endswith(('.pdf','.docx','.doc','.txt','.md','.html','.rst','.yaml','.yml')):return 'document'
    if p.endswith(('.py','.r','.rmd','.sql','.ipynb','.java','.js','.sh')):return 'code'
    if p.endswith(('.owl','.obo','.ttl','.rdf')):return 'ontology'
    return 'other'


def _mentions(text,*needles):
    t=text.lower();return any(n in t for n in needles)


def blockers(d,route,format_issues):
    """Why a non-ready entry is not ready: structured fields first, then the recorded problem notes (keyword match)."""
    state=d['optimization_status'];notes=list(d.get('missing_components',[]))+([d['review_note']] if d.get('review_note') else [])
    found=set()
    if state=='needs_preparation_or_permissions':
        if d['associated_download_permissions']['status']!='allowed':found.add('prep_rights')
        if d['benchmark_status']=='candidate_reference_review_and_frozen_split_required':found.add('prep_reference')
        if d['benchmark_status']=='control_only_not_independent_clinical_gold':found.add('prep_control')
        if format_issues:found.add('prep_format')
        if route in {'registration','owner'}:found.add('prep_access')
        for note in notes:
            if _mentions(note,'version','snapshot','mismatch','differ','versus',' vs ','absent','empty','not byte'):found.add('prep_version')
            if _mentions(note,'adapter','reader','separation','separate','interpretation','reconcil','isolate'):found.add('prep_adapter')
    elif state=='incomplete':
        if route in {'registration','owner'}:found.add('inc_access')
        for note in notes:
            if _mentions(note,'dua','credential','access','approval','agreement','registration','request','governance','safe haven'):found.add('inc_access')
            if _mentions(note,'licence','license','terms','rights','permission'):found.add('inc_licence')
            if _mentions(note,'dictionar','label','gold','reference','split','expert','annotation','mapping','vocabular','consensus','oracle','target list'):found.add('inc_reference')
            if _mentions(note,'participant','patient','records','cohort','source table','clinical table','snapshot','input'):found.add('inc_records')
            if _mentions(note,'hash','manifest','size','version','identity','reconcil','overlap','provenance','not verified','unverified'):found.add('inc_identity')
        if not found:found.add('inc_other')
    return sorted(found)


def metadata(d,policy,licenses,availability,access):
    benchmark=set()
    for use in d['uses']:
        relation=use['relation'].lower()
        if any(s in relation for s in ['mapping evaluation','harmonization evaluation','schema/value harmonization','drug-mapping evaluation']) or use['study'] in {'cde_mapper_hf','cdemapper_nih','passionate_2025','labbridge','aireadi_mapping_manuscript'}:benchmark.add('mapping')
        if 'extraction evaluation' in relation:benchmark.add('extraction')
        if any(s in relation for s in ['predictive','representation evaluation','generative']) or (relation=='introduced/evaluated' and use['study'] in {'meditab','ehrshot'}):benchmark.add('prediction')
    if not benchmark:benchmark.add('context')
    work=set()
    if d['optimization_status']=='needs_preparation_or_permissions':work.add('preparation')
    if d['optimization_status']=='incomplete' or d['missing_components']:work.add('missing')
    if d['associated_download_permissions']['status']!='allowed':work.add('rights')
    route=access(d)
    if route in {'registration','owner'}:work.add(route)
    if d['benchmark_status'] in {'development_only_reference_or_holdout_unresolved','candidate_reference_review_and_frozen_split_required'}:work.add('reference')
    for f in d['obtained_artifacts']:
        s=f['structure']
        if s.get('issues') or s['state'] in {'failed','not_dataset_response','needs_review'}:work.add('format')
    why=blockers(d,route,'format' in work)
    resource_ids={v['resource'] for v in d['associated_download_permissions']['decisions']}
    relevant=[r for r in licenses['resources'] if r['id'] in resource_ids and r.get('scope')!='software_only']
    families={license_family(r['license']) for r in relevant}
    if not relevant or resource_ids&{'unregistered_input','empty_input'}:families.add('unknown')
    pre=d['preaccess_screen']
    if pre.get('public_dataset_or_route_agreement_reviewed') and route=='registration':families.add('dua')
    files=[{'path':f['path'],'bytes':f['bytes'],'sha256':f['sha256']} for f in d['obtained_artifacts']]
    prepared=[{'path':f['path'],'bytes':f['bytes']} for f in d['prepared_artifact_profiles']]
    formats=sorted({file_format(f['path']) for f in files+prepared}) or ['not_held']
    cll='direct' if d['id'] in policy['cll_direct'] else 'indirect' if d['id'] in policy['cll_indirect_laboratory'] else 'other'
    return {'id':d['id'],'name':d['name'],'priority':d['action_priority'],'ready':d['optimization_status'],'available':availability(d),'access':route,
            'benchmark':sorted(benchmark),'kind':[d['category']],'license':sorted(families),'license_names':sorted({r['license'] for r in relevant}),
            'work':sorted(work),'blocker':why,'relevance':[d['workflow_relevance']],'cll':[cll],'format':formats,
            'retained_bytes':d['associated_download_bytes'],'prepared_bytes':d['prepared_artifact_bytes'],
            'size_retained':[size_band(d['associated_download_bytes'])],'size_prepared':[size_band(d['prepared_artifact_bytes'])],
            'files':files,'prepared_files':prepared,'families':d['leakage_families'],
            'search':' '.join([d['id'],d['name'],d['scope'],d['workflow_relevance'],d['category']]+[u['study'] for u in d['uses']]+[s['title'] for s in d['study_details']]).lower()}
