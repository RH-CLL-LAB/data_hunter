/* Your studies and “Merge studies into a new study”: the list, the request the CLI validates, the preview from the local server. No data grants. */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.DataHunterMerge=api;})(typeof globalThis!=='undefined'?globalThis:this,()=>{
 'use strict';
 const KIND='datahunter_study_facts_merge_request';
 const FIELDS=['id','name','question','intended_use','population','targets','inclusion','exclusion'];
 function buildRequest(sources,brief,reviewed,preview){
  if(!Array.isArray(sources)||!sources.length)throw Error('Tick at least one study to merge.');
  if(new Set(sources).size!==sources.length)throw Error('A study is ticked twice.');
  const study={};for(const f of FIELDS)study[f]=String((brief||{})[f]||'').trim();
  if(!/^[a-z0-9][a-z0-9_-]{0,79}$/.test(study.id))throw Error('Give the new study an ID of lowercase letters, digits, hyphens or underscores (at most 80 characters, starting with a letter or digit).');
  if(sources.includes(study.id))throw Error('The new study ID is one of the ticked studies.');
  for(const f of ['name','question','intended_use'])if(!study[f])throw Error('Fill in the new study’s '+f.replace('_',' ')+'.');
  if(reviewed!==true)throw Error('Tick that you reviewed the preview.');
  return {schema_version:1,kind:KIND,created_at:new Date().toISOString(),sources:[...sources],new_study:study,reviewed:true,
   ...(preview&&preview.totals?{preview_totals:preview.totals}:{}),
   instructions:'Create study '+study.id+' with Scripts/merge_study_facts.py --request <this file> --apply: register the brief, then merge the facts of '+sources.join(', ')+' in that order (catalogue entries as pre-access entries, paper records, selection placements, terms screens with no rights granted, notices by hash, licence records with every operation unreviewed; the first study wins where two disagree), then build its catalogue, status document and site. No file holdings and no AI-use permission cross over; files follow through a download with reuse_held_files under the new study’s own gates.'};
 }
 function command(filename){return '.venv/bin/python Scripts/merge_study_facts.py --request '+filename+' --apply';}
 function fileName(id){return 'datahunter-merged-study-'+id+'.json';}
 function fetchProblem(status,contentType){
  if(String(contentType||'').includes('application/json'))return '';
  if(status===404)return 'this local server does not know /api/merge-plan: it was started before this control existed, so restart it (python Scripts/serve.py)';
  return 'the local server answered '+status+' with '+(contentType||'no content type')+' instead of JSON';
 }
 function prefill(studies){
  // A suggested brief from the ticked studies; every field stays editable, and the merged study's permissions bind to what is saved.
  const label=w=>w.id,part=(w,f)=>((w.brief||{})[f]||'').trim();  // IDs keep the labels short; the names are in the list above
  const join=f=>studies.map(w=>part(w,f)).filter(Boolean).map((t,i)=>'['+label(studies.filter(w=>part(w,f))[i])+'] '+t).join('\n\n');
  return {id:(studies.map(w=>w.id).join('-and-').slice(0,60)+'-merged').toLowerCase(),name:('Merged: '+studies.map(label).join(' + ')).slice(0,160),question:join('question'),intended_use:join('intended_use'),population:join('population'),targets:join('targets'),inclusion:join('inclusion'),exclusion:join('exclusion')};
 }
 function human(bytes){if(!bytes)return '0 B';const u=['B','KB','MB','GB','TB'];let i=0,v=bytes;while(v>=1000&&i<u.length-1){v/=1000;i++;}return v.toFixed(i===0?0:1)+' '+u[i];}
 function planText(preview){
  if(!preview)return '';
  if(preview.error)return 'The preview could not be computed: '+preview.error;
  const lines=[];
  for(const s of preview.sources||[])lines.push(s.study+': '+s.datasets+' catalogue entries ('+s.held_datasets+' held), '+s.papers+' papers, '+s.licence_records+' licence records, '+s.terms_screens+' terms screens, '+s.notices+' notices, '+s.held_files.toLocaleString()+' held files ('+human(s.held_bytes)+')');
  for(const [kind,ids] of Object.entries(preview.overlaps||{}))if(ids.length)lines.push(kind+' in more than one study ('+ids.length+'): the first ticked study wins, a differing later record is reported and left out: '+ids.slice(0,25).join(', ')+(ids.length>25?' …':''));
  if(preview.files)lines.push(preview.files);
  lines.push('No file holdings and no AI-use permission cross over; licence records arrive unreviewed.');
  return lines.join('\n');
 }
 function mount(){
  const list=document.querySelector('[data-study-list]'),panel=document.querySelector('[data-merge-panel]'),node=document.getElementById('research-study-data');if(!list||!panel||!node)return;
  const seed=JSON.parse(node.textContent),workspaces=seed.workspaces||[],rows=list.querySelector('[data-study-rows]'),note=list.querySelector('[data-study-list-note]');
  const form=panel.querySelector('[data-merge-form]'),status=panel.querySelector('[data-merge-status]'),plan=panel.querySelector('[data-merge-plan]'),reviewed=panel.querySelector('[data-merge-reviewed]'),preview=panel.querySelector('[data-merge-preview]');
  const order=[];let touched=false,lastPreview=null;
  const esc=t=>String(t??'');
  for(const w of workspaces){
   const tr=document.createElement('tr'),pick=document.createElement('td'),box=document.createElement('input'),name=document.createElement('td'),held=document.createElement('td'),reports=document.createElement('td'),open=document.createElement('td');
   box.type='checkbox';box.value=w.id;box.setAttribute('data-merge-pick','');box.setAttribute('aria-label','Merge '+esc(w.name));box.title='Tick this study to merge its facts into the new study; tick order decides which facts win.';
   box.addEventListener('change',()=>{const i=order.indexOf(w.id);if(box.checked&&i<0)order.push(w.id);if(!box.checked&&i>=0)order.splice(i,1);lastPreview=null;plan.hidden=true;if(!touched)fill(prefill(order.map(id=>workspaces.find(x=>x.id===id))));});
   pick.append(box);name.innerHTML='<strong></strong><br><small class="small"></small>';name.querySelector('strong').textContent=esc(w.name);name.querySelector('small').textContent=w.id+(w.current?' · shown now':'')+(w.href?'':' · no site built yet');
   held.textContent=(w.held_files||0).toLocaleString();reports.textContent=String(w.reports||0);
   if(w.current){open.textContent='shown now';}
   else if(w.href){const a=document.createElement('a');a.className='plainbutton';a.href=w.href;a.textContent='Open';a.setAttribute('data-study-open','');a.title='Open this study’s own site; the Study dropdown at the top does the same.';open.append(a);}
   else open.textContent='—';
   tr.append(pick,name,held,reports,open);rows.append(tr);
  }
  if(!workspaces.length){note.textContent='No registered study workspace yet: register a study with the agent first.';panel.hidden=true;return;}
  note.textContent='Studies are registered through the agent from an exported study file; drafts that live only in this browser appear in the Study dropdown at the top.';
  function fill(values){for(const f of FIELDS){const el=form.elements[f];if(el&&values[f]!==undefined)el.value=values[f];}}
  form.addEventListener('input',()=>{touched=true;},{once:true});
  const brief=()=>{const b={};for(const f of FIELDS)b[f]=form.elements[f]?form.elements[f].value:'';return b;};
  preview.addEventListener('click',async()=>{
   lastPreview=null;plan.textContent='';plan.hidden=true;
   if(!order.length){status.textContent='Tick at least one study above.';return;}
   if(location.protocol==='file:'){status.textContent='The preview needs the local server (python Scripts/serve.py). The request can still be saved; the command prints the preview before anything is written.';return;}
   status.textContent='Asking the local server…';
   try{
    const response=await fetch('/api/merge-plan?sources='+encodeURIComponent(order.join(',')),{cache:'no-store'});
    const problem=fetchProblem(response.status,response.headers.get('content-type'));if(problem)throw Error(problem);
    lastPreview=await response.json();plan.textContent=planText(lastPreview);plan.hidden=false;
    status.textContent=lastPreview.error?'':'Preview only: nothing was written. Check the brief, tick the box and save the request.';
   }catch(error){status.textContent='The preview could not be fetched: '+error.message+'. The request can still be saved.';}
  });
  form.addEventListener('submit',event=>{
   event.preventDefault();
   try{
    const request=buildRequest(order,brief(),reviewed.checked,lastPreview),name=fileName(request.new_study.id);
    window.DataHunterSave.json(request,name);
    status.textContent='Saved '+name+'. Run '+command(name)+' with Claude Code or Codex: it creates and registers the study, merges the facts of '+order.join(', ')+' in that order and builds its site. Files follow through a download with “Reuse files another study already holds”.';
    if(window.DataHunterStudies){try{window.DataHunterStudies.logAction('note','Merge request saved: new study '+request.new_study.id+' from '+order.join(', ')+' (not created yet).');}catch(_){}}
   }catch(error){status.textContent=error.message;}
  });
 }
 if(typeof document!=='undefined'){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);else mount();}
 return {KIND,FIELDS,buildRequest,command,fileName,fetchProblem,prefill,planText,human};
});
