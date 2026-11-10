/* Merge facts from another study: the request the CLI validates, the plan from the local server. No data grants. */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.DataHunterMerge=api;})(typeof globalThis!=='undefined'?globalThis:this,()=>{
 'use strict';
 const KIND='datahunter_study_facts_merge_request';
 function buildRequest(source,target,reviewed,plan){
  if(!source||!target)throw Error('Choose the study whose facts are copied.');
  if(source===target)throw Error('Source and target are the same study.');
  if(reviewed!==true)throw Error('Tick that you reviewed the plan.');
  return {schema_version:1,kind:KIND,created_at:new Date().toISOString(),source,target,reviewed:true,
   ...(plan&&plan.counts?{plan_counts:plan.counts}:{}),
   instructions:'Copy the shareable facts of study '+source+' into study '+target+' with Scripts/merge_study_facts.py --request <this file> --apply: catalogue entries as pre-access entries, paper records, selection placements, terms screens with no rights granted, retained notices by hash and licence records with every operation unreviewed. A conflict stays untouched. No file holdings and no AI-use permission cross over; the target study’s licence review and the source-bound preflight still decide every AI operation.'};
 }
 function command(filename){return '.venv/bin/python Scripts/merge_study_facts.py --request '+filename+' --apply';}
 function fileName(source,target){return 'datahunter-merge-'+source+'-into-'+target+'.json';}
 function planText(plan){
  if(!plan)return '';
  if(plan.error)return 'The plan could not be computed: '+plan.error;
  return (plan.summary||[]).join('\n');
 }
 function mount(){
  const panel=document.querySelector('[data-merge-panel]'),node=document.getElementById('research-study-data');if(!panel||!node)return;
  const seed=JSON.parse(node.textContent),here=(seed.workspaces||[]).find(w=>w.current),others=(seed.workspaces||[]).filter(w=>!w.current);
  const select=panel.querySelector('[data-merge-source]'),status=panel.querySelector('[data-merge-status]'),plan=panel.querySelector('[data-merge-plan]'),reviewed=panel.querySelector('[data-merge-reviewed]'),save=panel.querySelector('[data-merge-save]'),preview=panel.querySelector('[data-merge-preview]'),target=panel.querySelector('[data-merge-target]');
  if(!here){panel.hidden=true;return;}  // the bootstrap site has no registered study to merge into
  target.textContent=here.name+' ('+here.id+')';
  for(const w of others){const o=document.createElement('option');o.value=w.id;o.textContent=w.name+' ('+w.id+')';select.append(o);}
  if(!others.length){status.textContent='No other registered study with a built site to copy facts from.';preview.disabled=true;save.disabled=true;return;}
  let lastPlan=null;
  preview.addEventListener('click',async()=>{
   lastPlan=null;plan.textContent='';plan.hidden=true;save.disabled=true;
   if(location.protocol==='file:'){status.textContent='The plan needs the local server (python Scripts/serve.py). The request can still be saved; the command prints the plan before anything is written.';save.disabled=false;return;}
   status.textContent='Computing the plan…';
   try{
    const response=await fetch('/api/merge-plan?source='+encodeURIComponent(select.value)+'&target='+encodeURIComponent(here.id),{cache:'no-store'});
    lastPlan=await response.json();plan.textContent=planText(lastPlan);plan.hidden=false;
    status.textContent=lastPlan.error?'':'Dry run: nothing was written. Review the plan, tick the box and save the request.';save.disabled=!!lastPlan.error;
   }catch(error){status.textContent='The plan could not be fetched ('+error.message+'). Is the local server running? The request can still be saved.';save.disabled=false;}
  });
  save.addEventListener('click',()=>{
   try{
    const request=buildRequest(select.value,here.id,reviewed.checked,lastPlan),name=fileName(select.value,here.id);
    window.DataHunterSave.json(request,name);
    status.textContent='Saved '+name+'. Run '+command(name)+' with Claude Code or Codex: it validates the request, prints the plan and applies it; conflicts stay untouched, no files and no AI-use permission cross over.';
    if(window.DataHunterStudies){try{window.DataHunterStudies.logAction('note','Merge request saved: facts of '+select.value+' into '+here.id+' (not applied yet).');}catch(_){}}
   }catch(error){status.textContent=error.message;}
  });
 }
 if(typeof document!=='undefined'){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);else mount();}
 return {KIND,buildRequest,command,fileName,planText};
});
