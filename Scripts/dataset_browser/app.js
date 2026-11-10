/* Local, dependency-free navigation. No requests, uploads, analytics or model calls. */
(() => {
  'use strict';
  const save=window.DataHunterSave,studies=window.DataHunterStudies;
  const initialParams=new URLSearchParams(location.search);
  if(document.querySelector('[data-home]')&&([...initialParams.keys()].some(k=>k==='q'||k==='pick'||k.startsWith('f.')||['view','priority','ready','access','available'].includes(k))||location.hash==='#catalogue')){
    location.replace('catalogue.html'+location.search+location.hash);return;
  }
  const back=document.querySelector('[data-back-selection]');
  if(back&&initialParams.get('selection')){back.href='../catalogue.html?'+initialParams.get('selection')+'#catalogue';back.textContent='Back to your selection';}
  const plural=(n,one,many)=>n+' '+(n===1?one:many);
  // Status wording comes from the page payload (FACETS in filter_metadata.py), so every page says the same thing.
  let permissionNames={};const useLabels=payload=>{permissionNames=payload.facets?.license_status?.[1]||{};};
  const table=document.querySelector('[data-catalogue]');
  let getAgentRows=()=>[],hiddenPicks=()=>0,agentContext=null,singleDataset=false;
  // Ticks survive filtering, so every action names how many ticked datasets are off-screen.
  const hiddenNote=()=>{const n=hiddenPicks();return n?` Includes ${plural(n,'ticked dataset','ticked datasets')} hidden by the current filters.`:'';};
  function actionState(){
    const rows=getAgentRows(),held=rows.filter(r=>r.files.length);
    const scope=document.querySelector('[data-action-scope]');
    if(scope)scope.textContent=!rows.length?'Tick datasets in the table to enable these actions.':singleDataset?(held.length?'This dataset has retained files.':'No retained files: file actions are unavailable.'):`${plural(rows.length,'ticked dataset','ticked datasets')} · ${held.length} with retained files · ${rows.length-held.length} without. File actions use the held ones only.`+hiddenNote();
    document.querySelectorAll('details.actionmenu').forEach(m=>{m.hidden=singleDataset&&!held.length;});
    document.querySelectorAll('[data-held-action]').forEach(b=>{b.disabled=!held.length;b.title=held.length?'Create a task for the held datasets':singleDataset?'This dataset has no retained files':rows.length?'No retained files among the ticked datasets':'Tick datasets with retained files first';});
    document.querySelectorAll('[data-save-agent],[data-file-inventory]').forEach(b=>{b.disabled=!rows.length;});
  }
  if(table){
    const payload=JSON.parse(document.getElementById('selection-data').textContent);
    const model=window.DataHunterSelection;
    useLabels(payload);
    const annotateAll=()=>payload.records.map(r=>studies.annotate(r,studies.current()));
    const records=annotateAll();
    for(const tag of new Set(records.flatMap(r=>r.project_tags))){
      if(payload.facets.project_tags[1][tag])continue;payload.facets.project_tags[1][tag]=tag;
      const fieldset=document.querySelector('[data-facet="project_tags"]').closest('fieldset'),box=document.createElement('div'),label=document.createElement('label'),input=document.createElement('input'),span=document.createElement('span'),count=document.createElement('small');
      box.className='facetoption';label.className='checkoption';input.type='checkbox';input.dataset.facet='project_tags';input.value=tag;span.textContent=tag;count.dataset.optionCount='project_tags:'+tag;label.append(input,span,count);box.append(label);fieldset.append(box);
    }
    agentContext={...payload.context,research_study:studies.profile(studies.current())};
    // The static count describes the default study; other studies need their own permission review.
    const heroReady=document.querySelector('[data-hero-ready]');if(heroReady)heroReady.textContent=records.filter(r=>r.ready==='ready_scoped').length;
    const rows=new Map([...table.querySelectorAll('tbody tr[data-id]')].map(row=>[row.dataset.id,row]));
    for(const record of records){
      const row=rows.get(record.id),label=row.querySelector('[data-row-permission]');
      if(label)label.textContent=permissionNames[record.license_status[0]];
      if(record.ready!==record.baseline_ready){const badge=row.querySelector('td[data-label="Development"] .badge');if(badge){badge.textContent='Study permission / preparation review';badge.className='badge warn';}}
    }
    const controls=[...document.querySelectorAll('[data-filter]')],checks=[...document.querySelectorAll('[data-facet]')];
    const $=selector=>document.querySelector(selector);
    const count=$('[data-count]'),previous=$('[data-previous]'),next=$('[data-next]'),pageLabel=$('[data-page]'),empty=$('[data-empty]'),sort=$('[data-sort]'),status=$('[data-export-status]');
    const bar=$('[data-selection-bar]'),pickAll=$('[data-pick-all]'),sidebar=$('[data-filter-panel]'),toggle=$('[data-filter-toggle]');
    const exportButton=$('[data-export]'),saveStudyButton=$('[data-save-study-selection]'),clearButton=$('[data-clear-picks]');
    const pageSize=25;let page=0,selected=[];
    const params=new URLSearchParams(location.search);
    // Ticks are an explicit choice: they survive searching and filtering until cleared.
    const picks=new Set(params.getAll('pick').filter(id=>rows.has(id)));
    getAgentRows=()=>records.filter(r=>picks.has(r.id));
    hiddenPicks=()=>{const visible=new Set(selected.map(r=>r.id));return [...picks].filter(id=>!visible.has(id)).length;};
    controls.forEach(el=>{if(params.has(el.dataset.filter))el.value=params.get(el.dataset.filter);});
    if([...sort.options].some(o=>o.value===params.get('sort')))sort.value=params.get('sort');
    checks.forEach(el=>{el.checked=params.getAll('f.'+el.dataset.facet).includes(el.value);});
    // Preserve older shared links while presenting checkbox-only controls.
    const legacy={priority:['priority','P1'],ready:['ready','ready_scoped'],access:['access','registration']};
    if(legacy[params.get('view')]){const [k,v]=legacy[params.get('view')];const c=checks.find(c=>c.dataset.facet===k&&c.value===v);if(c)c.checked=true;}
    for(const c of checks)if(c.checked)c.closest('.facetgroup').open=true;
    function state(){
      const facets={};checks.filter(c=>c.checked).forEach(c=>(facets[c.dataset.facet]||=[]).push(c.value));
      return {basic:Object.fromEntries(controls.map(el=>[el.dataset.filter,el.value.trim()])),facets};
    }
    function bytes(value){if(value===0)return '0 B';for(const [n,u] of [[1073741824,'GiB'],[1048576,'MiB'],[1024,'KiB']])if(value>=n)return (value/n).toFixed(2)+' '+u;return value+' B';}
    const readinessNames={ready_scoped:'Ready subsets',needs_preparation_or_permissions:'Preparation / rights review',incomplete:'Incomplete',reference_only:'Reference only',not_appropriate:'Excluded'};
    function exportRecords(){return picks.size?getAgentRows():selected;}
    function setStat(key,value){document.querySelectorAll('[data-stat="'+key+'"]').forEach(n=>{n.textContent=value;});}
    function summary(chosen){
      const s=model.summarize(chosen);
      for(const key of ['entries','ready','registration','action'])setStat(key,s[key]);
      setStat('retainedBytes',bytes(s.retainedBytes));setStat('picked',picks.size);
      const visible=new Set(chosen.map(r=>r.id)),hidden=[...picks].filter(id=>!visible.has(id)).length;
      $('[data-pick-hidden]').textContent=hidden?` (${hidden} hidden by filters)`:'';
      const readiness=$('[data-summary-readiness]');readiness.replaceChildren();
      for(const [key,label] of Object.entries(readinessNames)){const item=document.createElement('span');item.className='breakdownitem';item.textContent=label+': '+(s.counts.ready[key]||0);readiness.appendChild(item);}
      const evaluated=chosen.filter(r=>r.benchmark.some(b=>b!=='context')).length,l=s.counts.license_status;
      $('[data-summary-licences]').textContent=`${l.approved||0} with an approved local scope · ${l.source_reviewed||0} source reviews awaiting exact files · ${l.review_required||0} requiring review · ${l.prohibited||0} prohibited.`;
      $('[data-summary-evidence]').textContent=`${evaluated} reported evaluation families · ${s.owner} owner/request routes · ${s.uniqueFiles} unique retained artifacts · ${s.preparedFiles} prepared files (${bytes(s.preparedBytes)}, separate) · ${s.unknownSize} entries without retained-size evidence · ${s.overlapLabels} overlap labels (not independent cohorts).`;
      const exported=exportRecords();
      exportButton.textContent=picks.size?`Export ${picks.size} ticked`:`Export ${chosen.length} matching`;exportButton.disabled=!exported.length;
      saveStudyButton.textContent=picks.size?'Save ticked to study shortlist':'Save matching to study shortlist';saveStudyButton.disabled=!exported.length;
      clearButton.disabled=!picks.size;
      $('[data-export-scope]').textContent=picks.size?`Exports and actions use the ${plural(picks.size,'ticked dataset','ticked datasets')}, including any hidden by the current filters.`:`Nothing ticked: Export CSV uses all ${chosen.length} matching entries; actions need ticked datasets.`;
      const ticked=chosen.filter(r=>picks.has(r.id)).length;
      pickAll.checked=chosen.length>0&&ticked===chosen.length;pickAll.indeterminate=ticked>0&&ticked<chosen.length;
      actionState();
    }
    function chips(s){
      const target=$('[data-active-filters]');target.replaceChildren();
      function chip(label,remove){const b=document.createElement('button');b.type='button';b.className='filterchip';b.textContent=label+' ×';b.setAttribute('aria-label','Remove filter '+label);b.title='Remove this filter; the others stay.';b.addEventListener('click',()=>{remove();update();});target.appendChild(b);}
      controls.filter(c=>c.value).forEach(c=>chip((c.dataset.filter==='q'?'Search: ':'')+(c.tagName==='SELECT'?c.selectedOptions[0].text:c.value),()=>{c.value='';}));
      checks.filter(c=>c.checked).forEach(c=>chip(payload.facets[c.dataset.facet][1][c.value],()=>{c.checked=false;}));
      const active=target.childElementCount;
      if(!active)target.textContent='No filters applied — showing the full catalogue.';
      document.querySelectorAll('[data-filter-count]').forEach(n=>{n.textContent=active?'('+active+')':'';});
      document.querySelectorAll('[data-group-count]').forEach(n=>{const k=checks.filter(c=>c.checked&&c.dataset.facet===n.dataset.groupCount).length;n.textContent=k?k+' selected':'';});
    }
    function barHeight(){return bar&&getComputedStyle(bar).position==='sticky'?bar.offsetHeight:0;}
    function scrollToTable(){
      const top=table.getBoundingClientRect().top+scrollY-barHeight()-12;
      if(top<scrollY)scrollTo({top,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});
    }
    function update(reset=true,scroll=false){
      if(reset)page=0;
      const s=state();
      const chosen=records.filter(row=>model.matches(row,s));
      const rank={P1:0,P2:1,Maintain:2,'P3 / hold':3},mode=sort.value;
      chosen.sort((a,b)=>mode==='name'?a.name.localeCompare(b.name)
        :mode==='size'?(b.retained_bytes??-1)-(a.retained_bytes??-1)
        :(rank[a.priority]-rank[b.priority])||a.name.localeCompare(b.name));
      selected=chosen;
      const pages=Math.max(1,Math.ceil(chosen.length/pageSize));
      page=Math.max(0,Math.min(page,pages-1));
      rows.forEach(row=>{row.hidden=true;});
      chosen.slice(page*pageSize,(page+1)*pageSize).forEach(record=>{const row=rows.get(record.id);row.hidden=false;table.tBodies[0].appendChild(row);});
      count.textContent=`${chosen.length} of ${records.length} entries`;
      pageLabel.textContent=`${page+1} / ${pages}`;
      previous.disabled=page===0;next.disabled=page===pages-1;
      empty.hidden=chosen.length>0;
      summary(chosen);chips(s);
      checks.forEach(c=>{
        const n=records.filter(r=>model.matches(r,s,c.dataset.facet)&&(Array.isArray(r[c.dataset.facet])?r[c.dataset.facet]:[r[c.dataset.facet]]).includes(c.value)).length;
        document.querySelector('[data-option-count="'+c.dataset.facet+':'+c.value+'"]').textContent=n;
      });
      // Updating the URL is optional on file://; filtering itself works without it.
      const query=new URLSearchParams();query.set('study',studies.current().id);
      Object.entries(s.basic).forEach(([k,v])=>{if(v)query.set(k,v);});
      if(sort.value!=='priority')query.set('sort',sort.value);
      Object.entries(s.facets).forEach(([k,values])=>values.forEach(v=>query.append('f.'+k,v)));
      picks.forEach(id=>query.append('pick',id));
      const selectionQuery=query.toString();studies.remember(selectionQuery);
      rows.forEach((row,id)=>{
        row.querySelector('[data-pick]').checked=picks.has(id);
        row.querySelectorAll('a.title,[data-detail-link]').forEach(link=>{link.href='datasets/'+encodeURIComponent(id)+'.html?selection='+encodeURIComponent(selectionQuery)+(link.dataset.detailLink?'#'+link.dataset.detailLink:'');});
      });
      try{history.replaceState(null,'',location.pathname+'?'+query+location.hash);}catch(_){}
      document.documentElement.style.setProperty('--barh',barHeight()+'px');
      if(scroll)scrollToTable();
    }
    function setPick(id,on){if(on)picks.add(id);else picks.delete(id);}
    controls.forEach(el=>el.addEventListener(el.tagName==='INPUT'?'input':'change',()=>update()));
    checks.forEach(el=>el.addEventListener('change',()=>update()));
    table.addEventListener('change',event=>{const box=event.target.closest('[data-pick]');if(box){setPick(box.dataset.pick,box.checked);update(false);}});
    pickAll.addEventListener('change',()=>{selected.forEach(r=>setPick(r.id,pickAll.checked));update(false);});
    sort.addEventListener('change',()=>update());
    previous.addEventListener('click',()=>{page--;update(false,true);});
    next.addEventListener('click',()=>{page++;update(false,true);});
    $('[data-reset]').addEventListener('click',()=>{controls.forEach(el=>{el.value='';});checks.forEach(el=>{el.checked=false;});sort.value='priority';status.textContent='';update();});
    $('[data-expand-groups]').addEventListener('click',()=>document.querySelectorAll('.facetgroup').forEach(g=>{g.open=true;}));
    $('[data-collapse-groups]').addEventListener('click',()=>document.querySelectorAll('.facetgroup').forEach(g=>{g.open=false;}));
    $('[data-pick-filtered]').addEventListener('click',()=>{selected.forEach(r=>picks.add(r.id));update(false);status.textContent=`Ticked all ${selected.length} matching datasets.`;});
    clearButton.addEventListener('click',()=>{picks.clear();update(false);status.textContent='Ticks cleared.';});
    exportButton.addEventListener('click',()=>{
      const entries=exportRecords();save.csv(model.csv(entries),'datahunter-dataset-selection.csv');
      status.textContent=`Exported ${plural(entries.length,'catalogue entry','catalogue entries')}; metadata only. No dataset transfers were started.`;
    });
    saveStudyButton.addEventListener('click',()=>{
      const chosen=exportRecords().map(r=>r.id),existing=studies.current().dataset_ids||[],name=studies.current().name;
      // Ticks add to the shortlist; only the no-tick "save all matching" replaces it, and that asks first.
      if(!picks.size&&existing.length&&!confirm(`Nothing is ticked. Replace the ${plural(existing.length,'dataset','datasets')} shortlisted for “${name}” with all ${chosen.length} matching entries?`)){status.textContent='Shortlist unchanged.';return;}
      const ids=picks.size?[...new Set([...existing,...chosen])]:chosen,added=ids.length-existing.length;
      studies.shortlist(ids);studies.logAction('shortlist_saved',`${ids.length} datasets (${picks.size?added+' added':'replaced from matching'}): ${ids.slice(0,30).join(', ')}${ids.length>30?' …':''}`);
      const fresh=annotateAll();records.forEach((r,i)=>Object.assign(r,fresh[i]));update(false);
      const kept=studies.persisted()?'export the study to keep it outside this browser.':'browser storage is unavailable, so export the study now to keep it.';
      status.textContent=picks.size?`Added ${plural(Math.max(added,0),'dataset','datasets')} to the shortlist for “${name}” (${ids.length} total). Filter by “Study shortlist” to see it; ${kept}`
        :`Replaced the shortlist for “${name}” with ${plural(ids.length,'dataset','datasets')}. Filter by “Study shortlist” to see it; ${kept}`;
    });
    $('[data-copy-filter]').addEventListener('click',async()=>{
      try{await navigator.clipboard.writeText(location.href);status.textContent='Link copied. Local links need this same workspace or preview server.';}
      catch(_){status.textContent='Filters and ticks are saved in the address bar. Copy that URL to reuse this selection.';}
    });
    if(toggle)toggle.addEventListener('click',()=>{const open=!sidebar.classList.contains('open');sidebar.classList.toggle('open',open);toggle.setAttribute('aria-expanded',String(open));});
    addEventListener('resize',()=>document.documentElement.style.setProperty('--barh',barHeight()+'px'));
    // The filter panel can be dragged wider (or narrower); the width is a browser-local convenience.
    const resizer=$('[data-filter-resizer]'),layout=$('.cataloguelayout'),widthKey='datahunter.filter-width';
    if(resizer&&layout){
      const min=Number(resizer.getAttribute('aria-valuemin'))||240,max=Number(resizer.getAttribute('aria-valuemax'))||600,fallback=Number(resizer.getAttribute('aria-valuenow'))||330;
      const clamp=w=>Math.round(Math.min(max,Math.floor(layout.getBoundingClientRect().width/2),Math.max(min,w)));
      function setWidth(width,persist=true){
        width=clamp(width);document.documentElement.style.setProperty('--filterw',width+'px');resizer.setAttribute('aria-valuenow',String(width));
        if(persist)try{localStorage.setItem(widthKey,String(width));}catch(_){}
        document.documentElement.style.setProperty('--barh',barHeight()+'px');return width;
      }
      let saved=NaN;try{saved=Number(localStorage.getItem(widthKey));}catch(_){}
      setWidth(saved>=min&&saved<=max?saved:fallback,false);
      resizer.addEventListener('pointerdown',event=>{
        if(event.button!==0)return;
        const startX=event.clientX,startWidth=sidebar.getBoundingClientRect().width;
        resizer.setPointerCapture(event.pointerId);layout.classList.add('resizing');event.preventDefault();
        const move=e=>setWidth(startWidth+e.clientX-startX,false);
        const stop=()=>{layout.classList.remove('resizing');resizer.removeEventListener('pointermove',move);setWidth(sidebar.getBoundingClientRect().width);};
        resizer.addEventListener('pointermove',move);
        resizer.addEventListener('pointerup',stop,{once:true});resizer.addEventListener('pointercancel',stop,{once:true});
      });
      resizer.addEventListener('dblclick',()=>{setWidth(fallback);try{localStorage.removeItem(widthKey);}catch(_){}});
      resizer.addEventListener('keydown',event=>{
        const current=sidebar.getBoundingClientRect().width,step=event.shiftKey?40:16;
        const target={ArrowLeft:current-step,ArrowRight:current+step,Home:fallback,PageDown:min,PageUp:max,End:max}[event.key];
        if(target===undefined)return;event.preventDefault();setWidth(target);
      });
    }
    update();
  }
  // Copy the text of another element (citation, BibTeX); falls back to a hint when the clipboard is unavailable.
  document.querySelectorAll('[data-copy-text]').forEach(button=>button.addEventListener('click',async()=>{
    const source=document.getElementById(button.dataset.copyText),status=document.querySelector('[data-copy-status]');if(!source)return;
    try{await navigator.clipboard.writeText(source.textContent.trim());if(status)status.textContent='Copied.';}
    catch(_){source.hidden=false;if(status)status.textContent='Clipboard unavailable; select and copy the text above.';}
  }));
  const singleAgent=document.getElementById('agent-data');
  if(singleAgent){
    const p=JSON.parse(singleAgent.textContent);singleDataset=true;useLabels(p);
    const annotated=()=>p.records.map(r=>studies.annotate(r,studies.current()));
    const permissionLabel=document.querySelector('.license-status>strong');
    if(permissionLabel){const state=annotated()[0].license_status[0];permissionLabel.textContent='Active study local AI permission: '+(permissionNames[state]||state);
      const banner=document.querySelector('[data-permission-banner]');if(banner)banner.className='license-status tone-'+({approved:'approved',source_reviewed:'ref',review_required:'review',prohibited:'prohibited'}[state]||'review');}
    agentContext={...p.context,research_study:studies.profile(studies.current())};getAgentRows=annotated;
  }
  actionState();
  // Action menus close on outside click or Escape, like a native menu.
  document.addEventListener('click',event=>{document.querySelectorAll('details.actionmenu[open]').forEach(menu=>{if(!menu.contains(event.target))menu.open=false;});});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')document.querySelectorAll('details.actionmenu[open]').forEach(menu=>{const focused=menu.contains(document.activeElement);menu.open=false;if(focused)menu.querySelector('summary').focus();});});
  const agentDialog=document.querySelector('[data-agent-dialog]');
  let generatedDownload=null,generatedDownloadRows=[];
  function resetDownloadPrompt(){
    if(!agentDialog)return;
    generatedDownload=null;generatedDownloadRows=[];
    agentDialog.querySelector('[data-copy-agent]').disabled=true;agentDialog.querySelector('[data-confirm-agent]').disabled=true;
    agentDialog.querySelector('[data-agent-preview-box]').hidden=true;
  }
  if(agentDialog){agentDialog.addEventListener('input',resetDownloadPrompt);agentDialog.addEventListener('change',resetDownloadPrompt);}
  document.querySelectorAll('[data-save-agent]').forEach(button=>button.addEventListener('click',()=>{
    const rows=getAgentRows();
    if(!rows.length){document.querySelector('[data-agent-message]').textContent='Tick at least one dataset.';return;}
    document.querySelector('[data-agent-count]').textContent=`${plural(rows.length,'dataset','datasets')}: ${rows.slice(0,8).map(r=>r.id).join(', ')}${rows.length>8?' …':''}.`+hiddenNote();
    resetDownloadPrompt();document.querySelector('[data-agent-dialog-message]').textContent='';agentDialog.showModal();
  }));
  document.querySelectorAll('[data-cancel-agent]').forEach(button=>button.addEventListener('click',()=>agentDialog.close()));
  document.querySelectorAll('[data-generate-agent]').forEach(button=>button.addEventListener('click',()=>{
    const message=document.querySelector('[data-agent-dialog-message]');
    try{
      const rows=getAgentRows(),acknowledgements=[...document.querySelectorAll('[data-agent-risk]:checked')].map(c=>c.value);
      const spec=window.DataHunterSelection.agentInstructions(rows,agentContext,{acknowledgements,downloadScope:agentDialog.querySelector('[name="agent-download-scope"]:checked').value,licenseMode:document.querySelector('[name="agent-license-mode"]:checked').value,acquireUnresolvedRights:document.querySelector('[data-agent-unresolved]')?.checked===true,directHoldings:document.querySelector('[data-agent-direct-holdings]')?.checked===true,reuseHeldFiles:document.querySelector('[data-agent-reuse-held]')?.checked===true,datasetGB:document.querySelector('[data-agent-dataset]').value,maxGB:document.querySelector('[data-agent-max]').value,totalGB:document.querySelector('[data-agent-total]').value});
      generatedDownload=spec;generatedDownloadRows=rows;
      agentDialog.querySelector('[data-agent-preview]').textContent=window.DataHunterSelection.agentPrompt(rows,spec);
      const preview=agentDialog.querySelector('[data-agent-preview-box]');preview.hidden=false;preview.open=true;
      agentDialog.querySelector('[data-copy-agent]').disabled=false;agentDialog.querySelector('[data-confirm-agent]').disabled=false;
      message.textContent='Prompt generated for '+({review_only:'investigation only',minimal:'the minimal useful package',full:'the full provider release'}[spec.download_scope])+'. Review it below before copying or saving.';
    }catch(error){message.textContent=error.message;}
  }));
  document.querySelectorAll('[data-copy-agent]').forEach(button=>button.addEventListener('click',async()=>{
    if(!generatedDownload)return;
    try{await navigator.clipboard.writeText(agentDialog.querySelector('[data-agent-preview]').textContent);agentDialog.querySelector('[data-agent-dialog-message]').textContent='Prompt copied. Paste it into Claude Code or Codex to start the task.';}
    catch(_){agentDialog.querySelector('[data-agent-dialog-message]').textContent='Select and copy the generated prompt below.';}
  }));
  document.querySelectorAll('[data-confirm-agent]').forEach(button=>button.addEventListener('click',()=>{
    if(!generatedDownload)return;
    save.csv(window.DataHunterSelection.agentCSV(generatedDownloadRows,generatedDownload),'datahunter-download-instructions.csv');
    studies.logAction('download_instructions_prepared',`Task CSV saved: scope ${generatedDownload.download_scope}, licence ${generatedDownload.license_mode}, ${generatedDownloadRows.length} datasets: ${generatedDownloadRows.map(r=>r.id).join(', ').slice(0,800)}. No transfer started.`);
    document.querySelector('[data-agent-message]').textContent='Task CSV generated with scope '+generatedDownload.download_scope+'. Save it if prompted, then attach it in Claude Code or Codex. No transfer has started.';
    agentDialog.close();
  }));
  const operationDialog=document.querySelector('[data-operation-dialog]');let operation=null;
  const descriptions={remove:['Plan dataset removal','Prepare an exact file list and shared-dependency check for review. Keep catalogue, licence and acquisition history. No files are removed until you confirm the concrete plan; permanent deletion needs separate confirmation.'],verify:['Verify retained files','Check file presence, byte sizes and SHA-256 against the current catalogue. Report discrepancies; keep the source files unchanged.'],copy:['Copy retained files','Create a versioned working copy, deduplicating shared files and preserving notices. Originals and their registered paths stay in place.'],prepare_inputs:['Prepare workflow inputs','Prepare a versioned source-only input package after format and permission review. Keep references separate and record every transformation; report unresolved problems.'],relocate:['Plan a dataset move','Create a relocation plan, including affected catalogue paths, locks and run references. Actual movement follows review of that concrete plan; this task moves no originals.']};
  document.querySelectorAll('[data-dataset-action]').forEach(button=>button.addEventListener('click',()=>{
    button.closest('details.actionmenu')?.removeAttribute('open');
    operation=button.dataset.datasetAction;const rows=getAgentRows(),held=rows.filter(r=>r.files.length);
    document.querySelector('[data-operation-title]').textContent=descriptions[operation][0];
    document.querySelector('[data-operation-description]').textContent=descriptions[operation][1];
    document.querySelector('[data-operation-scope]').textContent=`Uses ${plural(held.length,'held dataset','held datasets')}`+(rows.length-held.length?`; skips ${rows.length-held.length} without retained files.`:'.')+hiddenNote();
    document.querySelector('[data-operation-destination-label]').hidden=['verify','remove'].includes(operation);
    document.querySelector('[data-operation-message]').textContent='';operationDialog.showModal();
  }));
  document.querySelectorAll('[data-cancel-operation]').forEach(b=>b.addEventListener('click',()=>operationDialog.close()));
  document.querySelectorAll('[data-confirm-operation]').forEach(b=>b.addEventListener('click',()=>{
    try{
      const plan=window.DataHunterSelection.operationCSV(getAgentRows(),agentContext,operation,document.querySelector('[data-operation-destination]').value);
      save.csv(plan,'datahunter-'+operation+'-instructions.csv');operationDialog.close();
      const held=getAgentRows().filter(r=>r.files.length);studies.logAction('file_task_prepared',`${operation} task CSV for ${held.length} held datasets: ${held.map(r=>r.id).join(', ').slice(0,800)}. No files changed.`);
      document.querySelector('[data-agent-message]').textContent='Task CSV generated. Pass it to Claude Code or Codex with agents/dataset-operations-agent.md. No source files were changed by this page.';
    }catch(error){document.querySelector('[data-operation-message]').textContent=error.message;}
  }));
  document.querySelectorAll('[data-file-inventory]').forEach(b=>b.addEventListener('click',()=>{
    b.closest('details.actionmenu')?.removeAttribute('open');
    save.csv(window.DataHunterSelection.inventoryCSV(getAgentRows()),'datahunter-file-inventory.csv');studies.logAction('file_inventory_exported',`${getAgentRows().length} datasets`);
    document.querySelector('[data-agent-message]').textContent='File inventory generated; shared paths appear once with all associated dataset IDs.';
  }));
  const licenseForm=document.querySelector('[data-license-review-form]');
  if(licenseForm){
    // The review date is the reviewer's date, not the build date of this page.
    const today=new Date(),local=`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
    licenseForm.querySelectorAll('[data-default-today]').forEach(i=>{if(!i.value)i.value=local;});
    const context={...JSON.parse(document.getElementById('license-review-context').textContent),research_study:studies.profile(studies.current())};
    const status=document.querySelector('[data-license-review-status]'),preview=document.querySelector('[data-license-review-preview]');
    const previewBox=document.querySelector('[data-license-review-preview-box]'),copy=document.querySelector('[data-copy-license-review]'),saveButton=document.querySelector('[data-save-license-review]');
    let decision=null,prompt='';
    function invalidateReview(){decision=null;prompt='';copy.disabled=true;saveButton.disabled=true;previewBox.hidden=true;status.textContent='Generate the decision after completing your review. No permission has changed.';}
    licenseForm.addEventListener('input',invalidateReview);licenseForm.addEventListener('change',invalidateReview);
    licenseForm.elements.purpose.value=context.research_study.intended_use;
    const noticePicker=licenseForm.elements.retained_notice;
    if(noticePicker)noticePicker.addEventListener('change',()=>{
      const notice=noticePicker.value===''?null:context.notices[Number(noticePicker.value)];if(!notice)return;
      for(const [key,value] of Object.entries({evidence_path:notice.snapshot,evidence_sha256:notice.sha256,evidence_url:notice.url,license_name:notice.license}))licenseForm.elements[key].value=value;
      invalidateReview();status.textContent='Evidence details filled. Review exact data coverage, rights and conditions before recording your decision.';
    });
    licenseForm.addEventListener('submit',event=>{
      event.preventDefault();if(!licenseForm.reportValidity())return;
      try{
        const data=new FormData(licenseForm),values=Object.fromEntries(data);
        values.operations=data.getAll('review_operations');values.asset_paths=data.getAll('review_assets');
        for(const key of ['underlying_rights_confirmed','conditions_satisfied','agreements_complete','whole_artifacts_reviewed'])values[key]=data.has(key);
        decision=window.DataHunterLicenseReview.request(context,values);prompt=window.DataHunterLicenseReview.prompt(decision);
        preview.textContent=prompt;previewBox.hidden=false;previewBox.open=true;copy.disabled=false;saveButton.disabled=false;
        status.textContent='Decision prepared — pending registration. Copy the prompt into Claude Code or Codex or attach the saved JSON. AI status changes only after validated registration and catalogue refresh.';
      }catch(error){status.textContent=error.message;}
    });
    copy.addEventListener('click',async()=>{
      if(!decision)return;
      try{await navigator.clipboard.writeText(prompt);studies.logAction('licence_review_prepared',`${context.dataset_id}: review prompt copied, pending registration.`);status.textContent='Review prompt copied. Pass it to Claude Code or Codex to validate and record your decision.';}
      catch(_){previewBox.open=true;status.textContent='Copy the displayed review prompt into Claude Code or Codex.';}
    });
    saveButton.addEventListener('click',()=>{
      if(!decision)return;
      save.json(decision,'datahunter-license-review-'+context.dataset_id+'.json');studies.logAction('licence_review_prepared',`${context.dataset_id}: decision file saved, pending registration; no permission changed.`);
      status.textContent='Decision file prepared. Attach it in Claude Code or Codex and ask to record it using agents/dataset-license-review-agent.md. Pending registration; no permission changed in this browser.';
    });
  }
  const intakeForm=document.querySelector('[data-intake-form]');
  if(intakeForm){
    const study=studies.current();
    if(new URLSearchParams(location.search).has('discover')){intakeForm.querySelector('[name="intake-task-type"][value="discovery"]').checked=true;intakeForm.elements.dataset_name.value='Dataset discovery: '+study.name;intakeForm.elements.purpose.value=studies.searchPurpose(study).slice(0,4000);document.getElementById('request')?.scrollIntoView({block:'start',behavior:'instant'});}
    const message=document.querySelector('[data-intake-status]');
    const preview=document.querySelector('[data-intake-preview]'),copyButton=document.querySelector('[data-copy-intake]'),saveButton=document.querySelector('[data-save-intake]');
    let generatedRequest=null,generatedPrompt='';
    function invalidatePrompt(){
      if(generatedRequest){message.textContent='Settings changed. Generate the prompt again before copying or saving.';preview.textContent='Settings changed — choose Generate prompt to update this preview.';}
      generatedRequest=null;generatedPrompt='';copyButton.disabled=true;saveButton.disabled=true;
    }
    intakeForm.addEventListener('input',invalidatePrompt);intakeForm.addEventListener('change',invalidatePrompt);
    function intakeType(){
      const discovery=intakeForm.querySelector('[name="intake-task-type"]:checked').value==='discovery';
      intakeForm.querySelector('[name="source_url"]').required=!discovery;
      document.querySelector('[data-intake-url-label]').textContent=discovery?'Seed dataset / paper URL (optional)':'Official public source URL';
      document.querySelector('[data-discovery-help]').hidden=!discovery;
    }
    intakeForm.querySelectorAll('[name="intake-task-type"]').forEach(c=>c.addEventListener('change',intakeType));intakeType();
    function intakeRequest(){
      if(!intakeForm.reportValidity())return null;
      const data=new FormData(intakeForm);
      return window.DataHunterIntake.request({research_study:studies.profile(studies.current()),task_type:data.get('intake-task-type'),dataset_name:data.get('dataset_name'),source_url:data.get('source_url'),purpose:data.get('purpose'),license_mode:data.get('intake-mode'),download_scope:'review_only',public_metadata_only:data.has('public_metadata_only')});
    }
    document.querySelector('[data-generate-intake]').addEventListener('click',()=>{
      try{
        const request=intakeRequest();if(!request)return;
        generatedRequest=request;generatedPrompt=window.DataHunterIntake.prompt(request);
        preview.textContent=generatedPrompt;preview.closest('details').open=true;
        copyButton.disabled=false;saveButton.disabled=false;
        message.textContent='Prompt generated below. Review it, then copy it into Claude Code or Codex or save the task file. No job has started.';
      }catch(error){message.textContent=error.message;}
    });
    copyButton.addEventListener('click',async()=>{
      if(!generatedRequest){message.textContent='Generate the prompt first.';return;}
      try{await navigator.clipboard.writeText(generatedPrompt);studies.logAction('intake_prompt_prepared',`${generatedRequest.task_type} prompt copied (${generatedRequest.license_mode}, ${generatedRequest.download_scope}).`);message.textContent='Prompt copied. Paste it into a Claude Code or Codex session in this repository to start the task.';}
      catch(_){preview.closest('details').open=true;message.textContent='Copy the generated prompt shown below and paste it into Claude Code or Codex.';}
    });
    intakeForm.addEventListener('submit',event=>{
      event.preventDefault();
      if(!generatedRequest){message.textContent='Generate the prompt first.';return;}
      save.json(generatedRequest,'datahunter-intake-request.json');studies.logAction('intake_prompt_prepared',`${generatedRequest.task_type} task file saved (${generatedRequest.license_mode}, ${generatedRequest.download_scope}).`);
      message.textContent='Task file generated. Save it if prompted, then attach it in Claude Code or Codex and use agents/dataset-intake-agent.md. No task has started.';
    });
  }
  document.querySelectorAll('[data-copy-prompt]').forEach(button=>button.addEventListener('click',async()=>{
    const status=document.querySelector('[data-prompt-status]');
    try{await navigator.clipboard.writeText('Use this active research study brief to adapt the example below; retain all licensing and transfer restrictions.\n'+JSON.stringify(studies.profile(studies.current()),null,2)+'\n\n'+document.getElementById(button.dataset.copyPrompt).textContent);status.textContent='Prompt copied. Replace the bracketed fields and paste it into Claude Code or Codex.';}
    catch(_){status.textContent='Select and copy the prompt text, then paste it into Claude Code or Codex.';}
  }));
  document.querySelectorAll('[data-help]').forEach(button=>{
    button.addEventListener('click',event=>{
      event.preventDefault();event.stopPropagation();
      const panel=document.getElementById(button.dataset.help),show=panel.hidden;
      const group=button.closest('.facetgroup');if(show&&group)group.open=true;
      panel.hidden=!show;button.setAttribute('aria-expanded',String(show));
    });
    button.addEventListener('keydown',event=>{
      if(event.key==='Escape'){document.getElementById(button.dataset.help).hidden=true;button.setAttribute('aria-expanded','false');}
    });
  });
})();
