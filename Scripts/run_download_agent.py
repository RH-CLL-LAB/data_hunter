#!/usr/bin/env python3
"""First download-agent prototype: resolve reviewed jobs, stage bytes, record outcomes.

No LLM/API is needed. Instructions select catalogue IDs, never executable commands
or arbitrary URLs. Technical acknowledgements cannot bypass access/review gates.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from zoneinfo import ZoneInfo

CODE_ROOT=Path(__file__).resolve().parents[1]
MAX_DATASET_BYTES=20_000_000_000
sys.path.insert(0,str(CODE_ROOT/'Scripts'))
sys.path.insert(0,str(CODE_ROOT/'src'))
from datahunter.workspace import workspace_root, require_workspace
ROOT=workspace_root(default=CODE_ROOT)
import acquire_research_resources as acquisition
from datahunter.data_rights import LicenseRegistry
from collections import Counter
from datahunter.research_studies import default_study, append_log as append_study_log, normalize as normalize_study, registered as registered_study, fingerprint as study_fingerprint

UNRESOLVED_RIGHTS_WARNING='Acquired under the explicit unresolved-rights option: open access, no licence decision recorded. AI use stays blocked until a licence review registers the terms.'
RISKS={
 'missing_provider_checksum':'No provider checksum/trusted identity is available. A local SHA-256 records the downloaded bytes but cannot prove they match the intended provider artifact.',
 'unknown_advertised_size':'The provider has not supplied an exact byte size. A hard transfer cap still applies; a failed or oversized transfer may leave a partial file.'
}
def read(p):return json.loads(Path(p).read_text())
def sha(p):
    with Path(p).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def save(p,data):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n')
def now():return datetime.now(timezone.utc).isoformat()


def read_instructions(path):
    path=Path(path)
    if path.suffix.lower()!='.csv':return read(path)
    with path.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        if not reader.fieldnames or len(reader.fieldnames)!=len(set(reader.fieldnames)):raise ValueError('invalid_csv_headers')
        rows=list(reader)
    common=['schema_version','kind','catalogue_sha256','license_mode','max_bytes_per_artifact','max_total_bytes','technical_risk_acknowledgements']
    if not rows or any(key not in reader.fieldnames for key in ['id',*common]):raise ValueError('not_agent_instructions_csv')
    common.extend(key for key in ['download_scope','download_requested','max_bytes_per_dataset','research_study','acquire_unresolved_rights','direct_holdings_for_acknowledged_risks','reuse_held_files'] if key in reader.fieldnames)
    if any(None in row or any(row[k]!=rows[0][k] for k in common) for row in rows):raise ValueError('inconsistent_csv_agent_settings')
    first=rows[0]
    scope=first.get('download_scope','minimal')
    if 'download_requested' in first and first['download_requested'] not in {'true','false'}:raise ValueError('invalid_download_requested')
    return {'schema_version':int(first['schema_version']),'kind':first['kind'],'catalogue_sha256':first['catalogue_sha256'],
            'license_mode':first['license_mode'],'max_bytes_per_artifact':int(first['max_bytes_per_artifact']),
            'max_total_bytes':int(first['max_total_bytes']),'max_bytes_per_dataset':int(first.get('max_bytes_per_dataset',MAX_DATASET_BYTES)),'technical_risk_acknowledgements':json.loads(first['technical_risk_acknowledgements']),
            'dataset_ids':[row['id'] for row in rows],'download_scope':scope,'download_requested':first.get('download_requested',str(scope!='review_only').lower())=='true',
            'agent_definition':'agents/dataset-download-agent.md' if scope=='minimal' else 'agents/dataset-intake-agent.md',
            'selected_context':rows,'source_format':'csv',**({'research_study':normalize_study(json.loads(first['research_study']))} if first.get('research_study') else {}),
            **({'acquire_unresolved_rights':first['acquire_unresolved_rights']=='true'} if first.get('acquire_unresolved_rights') in {'true','false'} else {}),
            **({'direct_holdings_for_acknowledged_risks':first['direct_holdings_for_acknowledged_risks']=='true'} if first.get('direct_holdings_for_acknowledged_risks') in {'true','false'} else {}),
            **({'reuse_held_files':first['reuse_held_files']=='true'} if first.get('reuse_held_files') in {'true','false'} else {})}


def validate_instructions(spec,catalogue_sha):
    if spec.get('research_study'):normalize_study(spec['research_study'])
    if spec.get('schema_version')!=1 or spec.get('kind') not in ('datahunter_download_instructions', 'medscrape_download_instructions'):raise ValueError('unsupported_download_instructions')
    ids=spec.get('dataset_ids')
    if not isinstance(ids,list) or not ids or any(not isinstance(i,str) or not re.fullmatch(r'[A-Za-z0-9_]+',i) for i in ids) or len(ids)!=len(set(ids)):raise ValueError('invalid_or_duplicate_dataset_ids')
    if spec.get('catalogue_sha256')!=catalogue_sha:raise ValueError('catalogue_changed_regenerate_instructions')
    if spec.get('license_mode') not in {'lenient','restrictive'}:raise ValueError('explicit_lenient_or_restrictive_license_mode_required')
    scope=spec.get('download_scope','minimal')
    if scope not in {'review_only','minimal','full'} or ('download_requested' in spec and spec['download_requested'] is not (scope!='review_only')):raise ValueError('invalid_or_inconsistent_download_scope')
    if 'acquire_unresolved_rights' in spec and (type(spec['acquire_unresolved_rights']) is not bool or (spec['acquire_unresolved_rights'] and scope=='review_only')):raise ValueError('invalid_acquire_unresolved_rights_option')
    if 'direct_holdings_for_acknowledged_risks' in spec and (type(spec['direct_holdings_for_acknowledged_risks']) is not bool or (spec['direct_holdings_for_acknowledged_risks'] and scope!='full')):raise ValueError('invalid_direct_holdings_option')
    if 'reuse_held_files' in spec and (type(spec['reuse_held_files']) is not bool or (spec['reuse_held_files'] and scope=='review_only')):raise ValueError('invalid_reuse_held_files_option')
    acknowledgements=spec.get('technical_risk_acknowledgements',[])
    if not isinstance(acknowledgements,list) or any(r not in RISKS for r in acknowledgements):raise ValueError('invalid_technical_risk_acknowledgement')
    for key in ['max_bytes_per_artifact','max_total_bytes']:
        if type(spec.get(key)) is not int or spec[key]<=0:raise ValueError('explicit_positive_byte_caps_required')
    dataset_cap=spec.get('max_bytes_per_dataset',MAX_DATASET_BYTES)
    if type(dataset_cap) is not int or not 0<dataset_cap<=MAX_DATASET_BYTES or spec['max_bytes_per_artifact']>dataset_cap:raise ValueError('maximum_dataset_size_is_20_decimal_GB')
    return set(acknowledgements)


def bound_jobs(d,manifest,locked,reviews,base,root):
    """Only exact current lock paths or explicit reviewed dataset aliases bind jobs."""
    ids=set()
    artifacts={f['path']:f['sha256'] for f in d['obtained_artifacts']}
    for id,entry in locked.items():
        path=(base/entry['path']).resolve()
        if path.is_relative_to(root) and artifacts.get(str(path.relative_to(root)))==entry['sha256']:ids.add(id)
    for review in reviews.get('reviews',[]):
        if review.get('id')==d['id']:ids.update(review.get('resource_aliases',[]))
    return [j for j in manifest['resources'] if j['id'] in ids]


def technical_risks(job,locked):
    old=locked.get(job['id']);trusted=old and old.get('url')==job.get('url') and old.get('path')==job.get('path')
    risks=[]
    if not trusted and not any(job.get(k) for k in ['provider_sha256','provider_md5','git_blob_sha1']):risks.append('missing_provider_checksum')
    if not trusted and type(job.get('expected_bytes')) is not int:risks.append('unknown_advertised_size')
    return risks


def license_preflight(job,locked,reviews,registry):
    return acquisition.transfer_license_preflight(job,locked,reviews,registry)


def process_job(job,locked,stage,original,acknowledgements,execute,caps,journal,used,license_report,reuse=None):
    if job.get('acquisition')!='direct':return {'id':job['id'],'status':'blocked_access','problem':job.get('reason','Provider access required')}
    if not job.get('url','').startswith('https://'):return {'id':job['id'],'status':'blocked_https_required'}
    old=locked.get(job['id'])
    if old:
        if old.get('url')!=job.get('url') or old.get('path')!=job.get('path'):
            return {'id':job['id'],'status':'blocked_locked_source_identity_changed'}
        held=acquisition.contained(original,job['path'])
        if held.exists():
            if sha(held)!=old['sha256'] or held.stat().st_size!=old['bytes']:return {'id':job['id'],'status':'blocked_existing_bytes_changed'}
            return {'id':job['id'],'status':'already_downloaded_verified','path':str(held),'sha256':old['sha256'],'bytes':old['bytes']}
    # Both the scope gate and exact metadata/download gate run before any request.
    try:review=acquisition.require_download_review(job,locked)
    except Exception as err:return {'id':job['id'],'status':'blocked_review_or_access','problem':str(err)}
    unresolved=license_report.get('status')!='allowed' and caps.get('acquire_unresolved_rights') is True and review.get('rights_unresolved_download') is True
    if license_report.get('status')!='allowed' and not unresolved:return {'id':job['id'],'status':'blocked_license','license_preflight':license_report}
    budget=job.get('max_bytes',64*1024**2)
    if type(budget) is not int or budget<=0 or budget>caps.get('max_bytes_per_dataset',MAX_DATASET_BYTES) or budget>caps['max_bytes_per_artifact'] or used[0]+budget>caps['max_total_bytes']:
        return {'id':job['id'],'status':'blocked_byte_budget','artifact_cap':budget,'reserved_total':used[0]}
    risks=technical_risks(job,locked)
    unaccepted=[r for r in risks if r not in acknowledgements]
    result={'id':job['id'],'technical_risks':risks,'warnings':[RISKS[r] for r in risks]+([UNRESOLVED_RIGHTS_WARNING] if unresolved else []),'download_review':review,'rights_unresolved':unresolved}
    if unaccepted:return {**result,'status':'needs_technical_risk_acknowledgement','unaccepted':unaccepted}
    used[0]+=budget
    if not execute:return {**result,'status':'eligible_dry_run','transfer_cap':budget}
    journal('transfer_authorized',{'resource_id':job['id'],'url':job['url'],'risks':risks,'acknowledged':sorted(acknowledgements),'hard_cap':budget})
    try:
        record=acquisition.acquire(job,locked,base=stage,execute=True,journal=journal,allow_unresolved_rights=unresolved,reuse=reuse,license_context={'mode':license_report.get('mode','lenient'),'study_id':license_report.get('study_id') or license_report.get('default_study') or 'baseline'})
        return {**result,'status':'downloaded_quarantined' if risks else 'downloaded_rights_unresolved' if unresolved else 'downloaded_staged_verified','record':record,
                'promotion':'Not added to the original acquisition lock or cleared for workflow AI; inspect and register the approved delivered version first.'}
    except Exception as err:return {**result,'status':'transfer_failed','problem':str(err),'partial_files_retained':True}


def run(instructions_path,execute=False,root=ROOT):
    root=Path(root).resolve();spec=read_instructions(instructions_path)
    receipt=read(root/'data/interim/dataset_selection/latest.json');catalogue_path=root/receipt['selection']
    if sha(catalogue_path)!=receipt['selection_sha256']:raise ValueError('catalogue_receipt_changed')
    acknowledgements=validate_instructions(spec,receipt['selection_sha256']);catalogue=read(catalogue_path);datasets={d['id']:d for d in catalogue['datasets']}
    if set(spec['dataset_ids'])-set(datasets):raise ValueError('unknown_dataset_ids')
    manifest=read(root/'configs/research_resources.json');locked={f['id']:f for f in read(root/'configs/research_resources.lock.json')['files']}
    reviews=read(root/'configs/dataset_download_reviews.json')
    study=spec.get('research_study')
    if study:
        current=registered_study(root,study['id'])
        if not current or study_fingerprint(current)!=study_fingerprint(study):raise ValueError('register_current_research_study_before_downloading')
    licenses=LicenseRegistry(root/'configs/data_licenses.json',root/'configs/data_license_assets.json',root=root,mode=spec['license_mode'],study_id=study['id'] if study else None)
    acquisition.validate_scope(manifest)
    identities=[j['id'] for j in manifest['resources']]
    paths=[j['path'] for j in manifest['resources'] if j.get('acquisition')=='direct']
    if len(set(identities))!=len(identities) or len(set(paths))!=len(paths):raise ValueError('duplicate_manifest_identity_or_path')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');run_dir=root/'runs/downloads'/stamp;run_dir.mkdir(parents=True)
    save(run_dir/'instructions.json',spec)
    shutil.copyfile(instructions_path,run_dir/('original-instructions.csv' if Path(instructions_path).suffix.lower()=='.csv' else 'original-instructions.json'))
    for p in [Path(__file__),CODE_ROOT/'Scripts/acquire_research_resources.py']:
        shutil.copyfile(p,run_dir/p.name)
    stage=root/'data/quarantine/download_agent'/stamp;original=root/'data/acquisitions'
    def evidence_key(p):return p.relative_to(root).as_posix() if p.resolve().is_relative_to(root) else p.relative_to(CODE_ROOT).as_posix()  # tool code is recorded by its code-root path
    evidence_sources={evidence_key(p):p for p in [catalogue_path,root/'configs/research_resources.json',root/'configs/research_resources.lock.json',root/'configs/dataset_download_reviews.json',root/'configs/resource_scope.json',root/'configs/data_licenses.json',root/'configs/data_license_assets.json',CODE_ROOT/'src/datahunter/data_rights.py']}
    role=CODE_ROOT/'agents/dataset-download-agent.md'
    if role.is_file():evidence_sources['agents/dataset-download-agent.md']=role
    evidence={relative:sha(source) for relative,source in evidence_sources.items()}
    for relative,source in evidence_sources.items():
        target=run_dir/'evidence'/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    def journal(kind,details):
        with (run_dir/'operations.jsonl').open('a') as stream:stream.write(json.dumps({'at':now(),'kind':kind,'details':details},ensure_ascii=False)+'\n')
    journal('run_started',{'execute':execute,'dataset_ids':spec['dataset_ids'],'license_mode':licenses.mode,'evidence':evidence,'technical_acknowledgements':sorted(acknowledgements),'reuse_held_files':spec.get('reuse_held_files') is True})
    for r in sorted(acknowledgements):print('WARNING: '+RISKS[r],flush=True)
    results=[];used=[0];completed={}
    reuse=acquisition.holdings_of(acquisition.workspaces_with_holdings_near(root)) if spec.get('reuse_held_files') is True else None  # the saved option: copy what another study of this checkout holds, under this study's own gates
    for id in spec['dataset_ids']:
        d=datasets[id];jobs=bound_jobs(d,manifest,locked,reviews,original,root)
        dataset_license=licenses.check([f['sha256'] for f in d['obtained_artifacts']],['local_inference','local_embedding'])
        package_budget=sum(locked[j['id']]['bytes'] if j['id'] in locked and acquisition.contained(original,j['path']).is_file() else j.get('max_bytes',64*1024**2) for j in jobs if j.get('acquisition')=='direct')
        if spec.get('download_scope','minimal')!='review_only' and package_budget>spec.get('max_bytes_per_dataset',MAX_DATASET_BYTES):
            result={'dataset':id,'status':'blocked_dataset_size_limit','manifest_budget_upper_bound':package_budget,'max_bytes_per_dataset':spec.get('max_bytes_per_dataset',MAX_DATASET_BYTES),'remaining_work':d['missing_components'],'note':'Reviewed package byte budgets exceed the per-dataset cap. No subset was transferred for this dataset; review exact sizes/budgets rather than silently lowering a full-release request.'}
        elif spec.get('download_scope','minimal')!='minimal':
            result={'dataset':id,'status':'review_only_no_transfer' if spec['download_scope']=='review_only' else 'full_release_review_required',
                    'remaining_work':d['missing_components'],'note':'Pass this task to Claude Code or Codex with agents/dataset-intake-agent.md, which resolves the complete provider release with Scripts/resolve_full_release.py. This minimal-artifact executor does not infer complete provider-release scope or transfer any subset for a full request.'}
        elif not jobs:
            if d['obtained_artifacts']:
                bad=[f['path'] for f in d['obtained_artifacts'] if not (root/f['path']).is_file() or sha(root/f['path'])!=f['sha256']]
                result={'dataset':id,'status':'blocked_held_identity_changed' if bad else 'held_subset_verified','problem':bad,
                        'remaining_work':d['missing_components'],'note':'Held subset verified; this is not a full-corpus download or benchmark-completeness claim.'}
            else:result={'dataset':id,'status':'manual_access_or_metadata_review_required','access_route':d['access_route'],'next_action':d['next_action'],'remaining_work':d['missing_components']}
        else:
            outcomes=[]
            for job in jobs:
                if job['id'] not in completed:
                    try:
                        permission=license_preflight(job,locked,reviews,licenses)
                        completed[job['id']]={**process_job(job,locked,stage,original,acknowledgements,execute,spec,journal,used,permission,reuse),'license_preflight':permission}
                    except Exception as err:completed[job['id']]={'id':job['id'],'status':'failed','problem':str(err)}
                outcomes.append(completed[job['id']])
            result={'dataset':id,'status':'artifact_outcomes_recorded','artifacts':outcomes,'remaining_work':d['missing_components']}
        result['license_preflight']=dataset_license
        results.append(result);journal('dataset_outcome',result)
    report={'schema_version':1,'created_at':now(),'display_time_copenhagen':datetime.now(timezone.utc).astimezone(ZoneInfo('Europe/Copenhagen')).isoformat(),'mode':'execute' if execute else 'dry_run','license_mode':licenses.mode,'download_scope':spec.get('download_scope','minimal'),'max_bytes_per_dataset':spec.get('max_bytes_per_dataset',MAX_DATASET_BYTES),'dataset_results':results,'unique_artifacts':list(completed.values()),'reserved_transfer_bytes':used[0],
            'instructions_sha256':sha(run_dir/'instructions.json'),'original_instructions_sha256':sha(instructions_path),'source_code_sha256':{p.name:sha(run_dir/p.name) for p in [Path(__file__),root/'Scripts/acquire_research_resources.py']},
            'quarantine_directory':str(stage.relative_to(root)),'evidence':evidence,'model_calls':0,'ai_use_authorized_by_download':False,'global_acquisition_lock_modified':False,
            'limits':'Prototype supports the active reviewed research-resource manifest. Other sources remain held/manual/review outcomes; instructions cannot supply arbitrary transfer URLs or bypass agreements, rights, TLS, format/identity checks or byte caps.'}
    save(run_dir/'report.json',report)
    with (run_dir/'outcomes.csv').open('w') as stream:
        writer=csv.writer(stream);writer.writerow(['dataset','license_mode','license_status','artifact','status','path','bytes','remaining_work'])
        for result in results:
            for outcome in result.get('artifacts',[result]):
                record=outcome.get('record',outcome)
                writer.writerow([result['dataset'],licenses.mode,outcome.get('license_preflight',result['license_preflight'])['status'],outcome.get('id',''),outcome['status'],record.get('path',''),record.get('bytes',''),'; '.join(result['remaining_work'])])
    journal('run_finished',{'report_sha256':sha(run_dir/'report.json'),'dataset_count':len(results),'unique_artifacts':len(completed)})
    # Study log: every download-agent run, dry or executed, with per-dataset outcomes. Instructions
    # without a research study belong to the licence registry's default study.
    statuses=Counter(r['status'] for r in completed.values());moved=sum(r.get('record',{}).get('bytes',0) or 0 for r in completed.values() if str(r['status']).startswith('downloaded'))
    outcome='; '.join(f"{r['dataset']}: {r['status']}" for r in results)
    append_study_log(root,study['id'] if study else default_study(root),[{'at':now(),'action':'download_run','source':'run_download_agent.py','run':str(run_dir.relative_to(root)),
        'detail':(f"{'EXECUTED' if execute else 'Dry run'}: scope {spec.get('download_scope','minimal')}, licence {licenses.mode}, {len(results)} datasets, "
                  f"{moved} bytes transferred; artifacts {dict(statuses)}. {outcome}")[:1000]}])
    print(json.dumps({'mode':report['mode'],'license_mode':licenses.mode,'datasets':len(results),'artifact_statuses':{s:sum(r['status']==s for r in completed.values()) for s in sorted({r['status'] for r in completed.values()})},'report':str(run_dir/'report.json')}),flush=True);return report


if __name__=='__main__':
    require_workspace(ROOT)  # a clear message instead of a missing-file error when no study is active
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--instructions',required=True,type=Path);p.add_argument('--execute',action='store_true',help='Execute eligible reviewed jobs; default is dry run')
    args=p.parse_args();run(args.instructions,args.execute)
