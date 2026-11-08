"""Hash-bound acquisition dates; receipt check times are only held-by evidence."""
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Europe/Copenhagen')

def timestamp(value):
    if not isinstance(value,str):return None
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed if parsed.tzinfo else None
    except ValueError:return None

def date_index(root,locks):
    root=Path(root).resolve();index={};sources=[]
    def add(path,sha,value,basis,source):
        parsed=timestamp(value)
        if parsed:index.setdefault((path,sha),[]).append({'at':parsed.isoformat(),'basis':basis,'evidence':str(source.relative_to(root))})
    for lock_name,base in locks.items():
        base=Path(base);lock=root/lock_name
        if not lock.is_file():continue
        sources.append(lock);files=json.loads(lock.read_text())['files'];identities=set()
        for f in files:
            path=str((base/f['path']).relative_to(root));identities.add((path,f['sha256']))
            add(path,f['sha256'],f.get('acquired_at'),'artifact_acquisition_timestamp',lock)
        for name in ['download_manifest.json','acquisition-report.json']:
            receipt=base/name
            if not receipt.is_file():continue
            sources.append(receipt);doc=json.loads(receipt.read_text())
            for f in doc.get('files',[]):
                if not f.get('path') or not f.get('sha256'):continue
                path=str((base/f['path']).relative_to(root));key=(path,f['sha256'])
                if key not in identities:continue
                actual=f.get('acquired_at') or f.get('downloaded_at')
                if actual:add(path,f['sha256'],actual,'artifact_acquisition_timestamp',receipt)
                elif doc.get('downloaded_at'):add(path,f['sha256'],doc['downloaded_at'],'download_batch_timestamp',receipt)
                else:add(path,f['sha256'],doc.get('checked_at'),'receipt_held_by_only',receipt)
    return index,list(dict.fromkeys(sources))

def summarize(artifacts,index):
    entries=[]
    for f in artifacts:
        records=index.get((f['path'],f['sha256']),[])
        actual=[r for r in records if r['basis']!='receipt_held_by_only']
        chosen=min(actual or records,key=lambda r:timestamp(r['at'])) if records else None
        entries.append({'path':f['path'],'sha256':f['sha256'],**(chosen or {'at':None,'basis':'unknown','evidence':None})})
    actual=[timestamp(r['at']) for r in entries if r['at'] and r['basis']!='receipt_held_by_only']
    bounds=[timestamp(r['at']) for r in entries if r['at']]
    unknown=sum(r['at'] is None for r in entries)
    def day(value):return value.astimezone(TZ).strftime('%d %b %Y')
    first,last=(min(actual),max(actual)) if actual else (None,None)
    if not entries:label='Not obtained'
    elif actual:
        label=day(first)+((' – '+day(last)) if day(first)!=day(last) else '')
        if len(actual)<len(entries):label+=' · partial dates'
    elif bounds:label='Date unknown · recorded held by '+day(max(bounds))+(' · partial evidence' if unknown else '')
    else:label='Unknown — acquisition date not recorded'
    return {'label':label,'first_at':first.isoformat() if first else None,'last_at':last.isoformat() if last else None,
            'recorded_held_by':max(bounds).isoformat() if bounds else None,'recorded_acquisition_files':len(actual),
            'held_by_only_files':sum(r['basis']=='receipt_held_by_only' for r in entries),'unknown_files':unknown,
            'total_files':len(entries),'files':entries,
            'limits':'Dates concern the retained files/subset, not the full provider corpus. A receipt check time proves held-by only. No publication dates, directory names or file modification times are used.'}
