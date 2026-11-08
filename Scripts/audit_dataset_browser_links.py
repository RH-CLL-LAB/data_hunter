#!/usr/bin/env python3
"""Audit generated HTML links; bounded public requests, no credentials/data downloads."""
import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import re
from urllib.parse import unquote, urlsplit, urlunsplit
import httpx

CODE_ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(CODE_ROOT/'src'))
from datahunter.workspace import workspace_root
ROOT=workspace_root(default=CODE_ROOT)
SITE=ROOT/'docs/dataset-browser'
BASE=ROOT/'data/interim/dataset_browser_links'
LIMIT=131072
BINARY=re.compile(r'\.(pdf|zip|gz|tar|xz|csv|tsv|xlsx?|docx?|png|jpg|tiff?|parquet|rds|ttl|owl|obo)(?:$|\?)',re.I)


class Page(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.links=[];self.ids=[];self.idrefs=[];self.title='';self.in_title=False;self.current=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if 'id' in attrs:self.ids.append(attrs['id'])
        for key in ['aria-controls','aria-labelledby','aria-describedby']:
            for id in attrs.get(key,'').split():self.idrefs.append({'attribute':key,'id':id})
        if tag=='label' and attrs.get('for'):self.idrefs.append({'attribute':'for','id':attrs['for']})
        if tag=='title':self.in_title=True
        for key in ['href','src']:
            if key in attrs:self.links.append({'url':attrs[key],'tag':tag,'text':''})
        if tag=='a' and 'href' in attrs:self.current=self.links[-1]
    def handle_endtag(self,tag):
        if tag=='title':self.in_title=False
        if tag=='a':self.current=None
    def handle_data(self,data):
        if self.in_title:self.title+=data
        if self.current is not None:self.current['text']+=data


def scan():
    pages={};external=defaultdict(list);internal=[];issues=[]
    for p in sorted(SITE.rglob('*.html')):
        parser=Page();parser.feed(p.read_text());pages[p.resolve()]=parser
        if not parser.title:issues.append({'page':str(p.relative_to(ROOT)),'issue':'missing_title'})
        for id,n in Counter(parser.ids).items():
            if n>1:issues.append({'page':str(p.relative_to(ROOT)),'issue':'duplicate_id','id':id,'count':n})
        for ref in parser.idrefs:
            if ref['id'] not in parser.ids:issues.append({'page':str(p.relative_to(ROOT)),'issue':'missing_control_or_label_target',**ref})
    for path,page in pages.items():
        for link in page.links:
            parsed=urlsplit(link['url']);ref={'page':str(path.relative_to(ROOT)),'text':link['text'].strip(),'tag':link['tag']}
            if parsed.scheme in {'https','http'}:
                canonical=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,parsed.query,''))
                external[canonical].append({**ref,'original_url':link['url']});continue
            if parsed.scheme=='mailto':continue
            if parsed.scheme or link['url'].startswith('//'):
                issues.append({**ref,'issue':'unexpected_link_scheme','url':link['url']});continue
            target=(path.parent/unquote(parsed.path)).resolve() if parsed.path else path
            state='ok' if target.exists() else 'missing'
            if state=='ok' and parsed.fragment and target in pages and unquote(parsed.fragment) not in pages[target].ids:state='missing_anchor'
            record={**ref,'url':link['url'],'target':str(target.relative_to(ROOT)) if target.is_relative_to(ROOT) else str(target),'status':state}
            internal.append(record)
            if state!='ok':issues.append({**record,'issue':'internal_'+state})
    return pages,external,internal,issues


def classify(status,title,snippet,final):
    # CAPTCHA scripts/widgets on a valid page do not make the page inaccessible.
    # Classify challenge pages from their title rather than arbitrary asset text.
    challenge=any(t in title.lower() for t in ['just a moment','checking your browser','verify you are human','access denied','captcha','request unsuccessful','proof-of-work challenge','bot check'])
    if status in {401,403,429,451} or challenge:return 'access_or_bot_restricted'
    if status in {404,410}:return 'not_found'
    if status in {500,502,503,504}:return 'server_error'
    if status>=400:return 'http_error'
    if status>=300:return 'redirect_unresolved'
    if any(t in title.lower() for t in ['page not found','404 not found','doi not found','page does not exist','error - not found','file not found']):return 'soft_not_found'
    if any(t in title.lower() for t in ['sign in','sign-in','log in','login','log-in']) or re.search(r'/(login|signin|log-in)(?:[/?]|$)',final):return 'login_page'
    return 'reachable'


async def check(url,client,global_sem,hosts):
    result={'url':url,'checked_at':datetime.now(timezone.utc).isoformat(),'attempts':[]}
    async with hosts[urlsplit(url).netloc],global_sem:
        for attempt in range(2):
            try:
                # HEAD avoids acquiring archives and participant packages.
                response=await client.head(url)
                rec={'method':'HEAD','http_status':response.status_code,'final_url':str(response.url),'content_type':response.headers.get('content-type',''),'redirects':[{'status':r.status_code,'url':str(r.url)} for r in response.history]}
                result['attempts'].append(rec)
                status=response.status_code;final=str(response.url);title='';snippet='';read_bytes=0
                content_type=response.headers.get('content-type','').lower()
                binary=BINARY.search(url) or any(x in content_type for x in ['application/pdf','application/zip','octet-stream','image/','spreadsheet','wordprocessing'])
                # Only inspect small HTML/error responses. Never read source datasets.
                if not binary or status in {404,405,410,501}:
                    async with client.stream('GET',url,headers={'Range':f'bytes=0-{LIMIT-1}'}) as response:
                        content_type=response.headers.get('content-type','').lower();status=response.status_code;final=str(response.url)
                        if ('text/' in content_type or 'html' in content_type or status>=400) and not BINARY.search(final):
                            chunks=[]
                            async for chunk in response.aiter_bytes():
                                remaining=LIMIT-read_bytes
                                chunks.append(chunk[:remaining]);read_bytes+=min(len(chunk),remaining)
                                if read_bytes>=LIMIT:break
                            snippet=b''.join(chunks).decode('utf-8',errors='replace')
                            match=re.search(r'<title[^>]*>(.*?)</title>',snippet,re.I|re.S)
                            title=re.sub(r'\s+',' ',match[1]).strip() if match else ''
                        result['attempts'].append({'method':'GET_bounded','http_status':status,'final_url':final,'content_type':content_type,'body_bytes_read':read_bytes})
                result.update(http_status=status,final_url=final,title=title,content_type=content_type,
                              status=classify(status,title,snippet,final),body_bytes_read=read_bytes,
                              content_validation='Headers only; resource identity/content not established' if binary else 'Bounded HTML/error body; not an article/data-content or external-fragment validation')
                if result['status']=='server_error' and attempt==0:continue
                return result
            except (httpx.HTTPError,OSError) as err:
                result['attempts'].append({'error':type(err).__name__,'detail':str(err)[:350]})
                result.update(status='network_or_tls_error',error=type(err).__name__,detail=str(err)[:350])
    return result


async def run(args):
    pages,external,internal,issues=scan();stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');folder=BASE/stamp;folder.mkdir(parents=True)
    results=[]
    if args.reuse:
        previous=json.loads(Path(args.reuse).read_text());old={r['url']:r for r in previous['external']}
        for r in old.values():
            if r.get('http_status'):
                current=classify(r['http_status'],r.get('title',''),'',r.get('final_url',r['url']))
                if current!=r['status']:r['previous_classification']=r['status'];r['status']=current
    else:old={}
    pending=[u for u in external if u not in old or (args.retry_unresolved and old[u]['status'] not in {'reachable','login_page'})
             or (args.retry_transient and old[u]['status'] in {'server_error','network_or_tls_error','redirect_unresolved'})]
    results=[r for u,r in old.items() if u in external and u not in pending]
    report={'schema_version':1,'started_at':datetime.now(timezone.utc).isoformat(),'site':'docs/dataset-browser',
            'html_pages':len(pages),'external_unique':len(external),'external_occurrences':sum(map(len,external.values())),
            'internal_occurrences':len(internal),'internal_statuses':dict(Counter(r['status'] for r in internal)),
            'html_issues':issues,'internal':internal,'external':results,'external_references':dict(external),
            'limits':{'body_max_bytes_per_request':LIMIT,'credentials':False,'participant_downloads':0,'agreements_accepted':0,'external_fragments_not_validated':True},
            'html_sources':[{'path':str(p.relative_to(ROOT)),'sha256':sha256(p.read_bytes()).hexdigest()} for p in pages],
            'audit_code_sha256':sha256(Path(__file__).read_bytes()).hexdigest()}
    (folder/'audit_code.py').write_bytes(Path(__file__).read_bytes())
    def save():
        report['external_statuses']=dict(Counter(r['status'] for r in results));report['completed_external']=len(results)
        (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    save();print(json.dumps({'html_pages':len(pages),'external_to_check':len(pending),'internal_issues':len(issues),'report':str((folder/'report.json').relative_to(ROOT))}),flush=True)
    async with httpx.AsyncClient(follow_redirects=True,max_redirects=12,timeout=httpx.Timeout(18.0,connect=10.0),headers={'User-Agent':'Mozilla/5.0 (compatible; Data Hunter-LinkAudit/1.0; bounded public link check)','Accept':'text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8'},limits=httpx.Limits(max_connections=12,max_keepalive_connections=8)) as client:
        semaphore=asyncio.Semaphore(10);hosts=defaultdict(lambda:asyncio.Semaphore(2))
        tasks=[asyncio.create_task(check(u,client,semaphore,hosts)) for u in pending]
        for done in asyncio.as_completed(tasks):
            results.append(await done)
            if len(results)%20==0:save();print(json.dumps({'checked':len(results),'of':len(external),'statuses':report['external_statuses']}),flush=True)
    report['completed_at']=datetime.now(timezone.utc).isoformat();save()
    receipt={'report':str((folder/'report.json').relative_to(ROOT)),'sha256':sha256((folder/'report.json').read_bytes()).hexdigest(),'summary':{'html_pages':len(pages),'internal_occurrences':len(internal),'internal_issues':len(issues),'external_unique':len(external),'external_statuses':report['external_statuses']}}
    (BASE/'latest.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt['summary'],indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--reuse',help='Reuse previous URL results and check only new URLs');parser.add_argument('--retry-unresolved',action='store_true');parser.add_argument('--retry-transient',action='store_true');args=parser.parse_args();asyncio.run(run(args))
