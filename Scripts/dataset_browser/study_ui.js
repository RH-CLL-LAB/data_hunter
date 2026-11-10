/* Browser-local research projects, selections and annotations. No data grants. */
(() => {
 const node=document.getElementById('research-study-data');if(!node)return;
 const api=window.DataHunterStudies,seed=JSON.parse(node.textContent);api.init(seed.studies,seed.default_study);
 const base=node.dataset.base||'',switcher=document.querySelector('[data-study-switch]');
 function refresh(){
  switcher.replaceChildren();
  for(const study of api.all()){const o=document.createElement('option');o.value=study.id;o.textContent=study.name;o.selected=study.id===api.current().id;switcher.append(o);}
  // Other registered studies live in their own generated sites: choosing one opens that site.
  const others=(seed.workspaces||[]).filter(w=>!w.current&&w.href);  // a registered study without a built site cannot be opened yet
  if(others.length){const group=document.createElement('optgroup');group.label='Other registered studies';
   for(const w of others){const o=document.createElement('option');o.value='workspace:'+w.id;o.textContent=w.name;o.dataset.href=w.href;group.append(o);}
   switcher.append(group);}
  document.querySelectorAll('[data-active-study-name]').forEach(n=>n.textContent=api.current().name);
 }
 refresh();
 const LOG_LABELS={study_created:'Study created',study_registered:'Study registered',study_updated:'Study updated',study_imported:'Study imported',study_exported:'Study exported',
  shortlist_saved:'Shortlist saved',annotation_saved:'Annotation saved',intake_prompt_prepared:'Intake task prepared',discovery_run:'Discovery run',report_added:'Report added',
  download_instructions_prepared:'Download instructions prepared',file_task_prepared:'File task prepared',file_inventory_exported:'File inventory exported',licence_review_prepared:'Licence review prepared',note:'Note',
  download_run:'Download run',licence_review_registered:'Licence decision registered',file_task_run:'File operation run',study_removed:'Study removed from registry'};
 // Registered reports/log come from the registry seed; this browser's actions stay marked until registered.
 function renderRecords(){
  const reports=document.querySelector('[data-study-reports]'),log=document.querySelector('[data-study-log]');if(!reports||!log)return;
  const study=api.current();reports.replaceChildren();log.replaceChildren();
  for(const r of study.reports||[]){const li=document.createElement('li'),a=document.createElement('a');a.textContent=r.title+' ('+r.date+')';a.href=base+(r.html||'');li.append(a);reports.append(li);}
  if(!reports.childElementCount){const li=document.createElement('li');li.className='small';li.textContent='No registered reports yet.';reports.append(li);}
  const entries=[...(study.log||[]).map(e=>({...e,registered:true})),...(study.local_log||[]).map(e=>({...e,registered:false}))].sort((x,y)=>x.at.localeCompare(y.at)).reverse();
  for(const e of entries.slice(0,200)){
   const li=document.createElement('li'),when=document.createElement('time'),what=document.createElement('strong'),detail=document.createElement('span'),where=document.createElement('small');
   when.dateTime=e.at;when.textContent=new Date(e.at).toLocaleString();what.textContent=' '+(LOG_LABELS[e.action]||e.action)+' — ';detail.textContent=e.detail||'';
   where.textContent=e.registered?' · registered'+(e.source?' by '+e.source:'')+(e.run?' · '+e.run:''):' · this browser only — export and register to keep';
   if(!e.registered)li.className='unregistered';li.append(when,what,detail,where);log.append(li);
  }
  if(!entries.length){const li=document.createElement('li');li.className='small';li.textContent='No actions recorded for this study yet.';log.append(li);}
 }
 renderRecords();
 // Switching keeps you on the current page; the catalogue restores that study's saved view.
 switcher.addEventListener('change',()=>{
  if(switcher.value.startsWith('workspace:')){const target=(seed.workspaces||[]).find(w=>'workspace:'+w.id===switcher.value);if(target){location.href=target.href;return;}}
  api.activate(switcher.value);
  if(document.querySelector('[data-catalogue]')){location.href=base+'catalogue.html?'+api.query(switcher.value);return;}
  const url=new URL(location.href);url.searchParams.set('study',switcher.value);url.searchParams.delete('selection');location.href=url.href;
 });
 // Links to the explorer reopen this study's last filters and ticks.
 document.querySelectorAll('a[href$="catalogue.html"]').forEach(a=>{a.href=a.getAttribute('href')+'?'+api.query(api.current().id);});
 document.querySelectorAll('[data-new-study-link]').forEach(a=>a.href=base+'research-studies.html?study='+encodeURIComponent(api.current().id)+'#new-study');
 // Arriving at #new-study puts the cursor in the first field, so the link lands on the form itself.
 if(location.hash==='#new-study'){const first=document.querySelector('[data-new-study-form] input,[data-new-study-form] textarea');if(first){document.getElementById('new-study')?.scrollIntoView({block:'start',behavior:'instant'});first.focus({preventScroll:true});}}
 document.querySelectorAll('[data-study-search-link]').forEach(a=>a.href=base+'add-datasets.html?discover=1&study='+encodeURIComponent(api.current().id));
 const note=document.querySelector('[data-research-study-context]');
 if(note){note.textContent=api.current().id===api.defaultId()?'Study: '+api.current().name+'. Dataset files are shared; shortlists and annotations belong to this study.':'Study: '+api.current().name+'. Catalogue priority/relevance and historical readiness describe the default study “'+(api.all().find(x=>x.id===api.defaultId())||{}).name+'”. Review relevance and data permissions for this study; approvals are not inherited.';}
 if(note&&location.protocol==='file:')note.textContent+=' For reliable study switching across pages, use the HTTP preview described in the reading guide; local-file browser storage can be isolated per page.';
 // A study with nothing registered yet is told where its dataset search starts.
 if(note&&api.current().id!==api.defaultId()){const s=api.current();const empty=!Object.keys(s.dataset_annotations||{}).length&&!(s.dataset_ids||[]).length&&!(s.reports||[]).length;
  if(empty){note.append(' No dataset search is registered for this study yet; start one on ');const link=document.createElement('a');link.href=base+'add-datasets.html?discover=1&study='+encodeURIComponent(s.id);link.textContent='Search datasets';note.append(link,'.');}}
 if(note&&api.current().shadowed_registered)note.textContent+=' This browser holds a draft of this study whose brief differs from the registered one, so registered reports, log entries and permissions are not shown. Export the draft to update the registration, or archive it to see the registered study.';
 const unsaved=()=>api.persisted()?'':' Browser storage is unavailable, so this was not kept: export the study before leaving.';
 const form=document.querySelector('[data-new-study-form]');
 if(form){
  form.addEventListener('submit',event=>{
   event.preventDefault();if(!form.reportValidity())return;
   const message=document.querySelector('[data-new-study-status]');
   try{const result=api.save(Object.fromEntries(new FormData(form)));api.logAction('study_created','Created in Data Hunter; register it to make the brief and this log durable.');refresh();if(!result.persisted){message.textContent='Browser storage is unavailable. Export the study before leaving this page.';return;}location.href=base+'add-datasets.html?discover=1&study='+encodeURIComponent(result.study.id);}
   catch(error){message.textContent=error.message;}
  });
 }
 document.querySelectorAll('[data-export-study]').forEach(button=>button.addEventListener('click',()=>{
  api.logAction('study_exported','Study, shortlist, annotations and this browser log exported for registration.');window.DataHunterSave.json(api.exportRequest(),'datahunter-study-'+api.current().id+'.json');renderRecords();
 }));
 const importControl=document.querySelector('[data-import-study]');
 if(importControl)importControl.addEventListener('change',async()=>{
  const message=document.querySelector('[data-new-study-status]');
  const file=importControl.files&&importControl.files[0];if(!file)return;
  try{const value=JSON.parse(await file.text());if(!['datahunter_research_study','medscrape_research_study'].includes(value.kind)||value.schema_version!==1)throw Error('Select an exported study JSON.');
   const result=api.restore(value);api.logAction('study_imported','Imported from an exported study file; no data permissions imported.');renderRecords();
   refresh();message.textContent='Imported '+result.name+'. No data permissions were imported or granted.'+unsaved();
  }catch(error){message.textContent=error.message;}
  finally{importControl.value='';}
 });
 const brief=document.querySelector('[data-active-study-brief]');if(brief)brief.textContent=api.searchPurpose(api.current());
 const archive=document.querySelector('[data-archive-study]');
 if(archive){archive.disabled=api.current().id===api.defaultId();archive.addEventListener('click',()=>{api.archive();location.href=base+'research-studies.html?study='+encodeURIComponent(api.defaultId());});}
 const removeButton=document.querySelector('[data-remove-study]');
 if(removeButton){
  const current=api.current(),registeredStudy=api.isRegistered(current.id);removeButton.disabled=current.id===api.defaultId();
  removeButton.textContent=registeredStudy?'Retire study…':'Delete browser draft…';
  removeButton.addEventListener('click',async()=>{
   const message=document.querySelector('[data-new-study-status]');
   if(!registeredStudy){
    if(!confirm(`Delete the browser draft “${current.name}” permanently? Its shortlist, annotations and unregistered log entries are lost unless you exported them.`))return;
    api.remove();location.href=base+'research-studies.html?study='+encodeURIComponent(api.defaultId());return;
   }
   // A registered study is removed from the registry by the agent; the browser only drops its own overlay.
   const command='.venv/bin/python Scripts/register_research_study.py --remove '+current.id+' --apply';
   let copied=false;try{await navigator.clipboard.writeText(command);copied=true;}catch(_){}
   const dropped=api.remove();
   message.textContent=(copied?'Removal command copied. ':'Copy this command: '+command+' . ')+'Run it with Claude Code or Codex to remove “'+current.name+'” ; it stops being the active study and its folder studies/'+current.id+'/ with registries, data, reports and log is kept. '+(dropped.registered?'This browser’s own draft of the study was discarded.':'');
  });
 }
 const restore=document.querySelector('[data-restore-archived-study]');if(restore)restore.addEventListener('click',()=>{try{api.restoreArchived();location.href=base+'research-studies.html?study='+encodeURIComponent(api.current().id);}catch(error){document.querySelector('[data-new-study-status]').textContent=error.message;}});
 document.querySelectorAll('[data-annotation-form]').forEach(form=>{
  const kind=form.dataset.annotationKind,id=form.dataset.annotationId,key=kind==='dataset'?'dataset_annotations':'study_annotations';
  const a=api.annotation(api.current()[key]?.[id]);
  for(const name of ['relevance','role','note'])form.elements[name].value=a[name];form.elements.tags.value=a.tags.join(', ');
  const status=form.querySelector('[data-annotation-status]');
  form.addEventListener('submit',event=>{
   event.preventDefault();const values=Object.fromEntries(new FormData(form));values.tags=values.tags.split(',');
   try{api.setAnnotation(kind,id,values);api.logAction('annotation_saved',`${kind} ${id}: relevance ${values.relevance}, role ${values.role}`);status.textContent='Saved for “'+api.current().name+'” in this browser.'+unsaved();document.dispatchEvent(new CustomEvent('datahunter:annotations',{detail:{kind,id}}));renderRecords();}
   catch(error){status.textContent=error.message;}
  });
 });
})();
