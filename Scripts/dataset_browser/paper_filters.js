/* Filter papers by project annotations and their associated dataset evidence. */
(() => {
 const node=document.getElementById('paper-filter-data');if(!node)return;
 const api=window.DataHunterStudies,data=JSON.parse(node.textContent),datasets=new Map(data.datasets.map(d=>[d.id,d]));
 const papers=data.studies.map(s=>api.paper(s,datasets,api.current())),infos=new Map(),cards=new Map([...document.querySelectorAll('[data-study]')].map(c=>[c.id.slice(6),c]));
 const target=document.querySelector('[data-paper-filters]'),search=document.querySelector('[data-study-search]');
 // Option wording is shared with the catalogue filters (FACETS); only the group captions are paper-specific.
 const facet=key=>({...data.facets[key][1]});
 const groups={project_relevance:['Relevance to this research study',facet('project_relevance')],
 project_role:['Intended role',facet('project_role')],
 benchmark:['Reported task of linked datasets',facet('benchmark')],
 license_status:['At least one linked dataset has this permission status',{...facet('license_status'),no_dataset:'No linked dataset established'}],
 review_route:['Linked dataset licence review records',facet('review_route')],
 permission_coverage:['Permission coverage of linked datasets',{all_scopes:'All recorded linked scopes approved',some_scopes:'Some recorded linked scopes approved',none:'No recorded linked scope approved'}],
 project_tags:['Study annotation tags',Object.fromEntries([...new Set(papers.flatMap(p=>p.project_tags))].sort().map(t=>[t,t==='untagged'?'No study tags yet':t]))]};
 const linkedId=location.hash.startsWith('#study-')?location.hash.slice(7):'';
 const saved=papers.some(p=>p.id===linkedId)?{q:linkedId,facets:{}}:api.paperFilters();search.value=saved.q||'';
 const fieldsets={};
 function option(key,value,text){
  const label=document.createElement('label'),input=document.createElement('input'),span=document.createElement('span');label.className='checkoption';input.type='checkbox';input.dataset.paperFacet=key;input.value=value;input.checked=(saved.facets?.[key]||[]).includes(value);span.textContent=text;label.append(input,span);fieldsets[key].append(label);return input;
 }
 for(const [key,[label,options]] of Object.entries(groups)){
  const details=document.createElement('details'),summary=document.createElement('summary'),fieldset=document.createElement('fieldset');details.className='facetgroup';details.open=['benchmark','license_status','review_route'].includes(key);const caption=document.createElement('span');caption.textContent=label;summary.append(caption);fieldset.setAttribute('aria-label',label);details.append(summary,fieldset);
  fieldsets[key]=fieldset;
  for(const [value,text] of Object.entries(options)){if(option(key,value,text).checked)details.open=true;}
  target.append(details);
 }
 const explanation=document.createElement('p');explanation.className='small';explanation.textContent='Several ticks in one group match any option (OR); groups combine with AND. Dataset permission counts describe recorded source scopes, not whole-study clearance. A paper’s open-access licence does not license its datasets.';target.prepend(explanation);
 const controls=[...target.querySelectorAll('[data-paper-facet]')],reset=document.createElement('button');reset.type='button';reset.className='plainbutton';reset.textContent='Reset paper filters';reset.title='Clear every paper filter; the search text stays.';reset.addEventListener('click',()=>{controls.forEach(c=>c.checked=false);search.value='';update();});target.append(reset);
 function describe(p){
  const card=cards.get(p.id);
  if(!infos.has(p.id)){const info=document.createElement('p');info.className='small paper-licence-summary';card.querySelector('h3').after(info);infos.set(p.id,info);}
  infos.get(p.id).textContent=p.dataset_count?`${p.approved_count}/${p.dataset_count} linked dataset scopes approved for this study (source use, not paper licensing).`:'No linked dataset established.';
  p.search=(p.id+' '+card.querySelector('h3').textContent+' '+(card.querySelector('.doi')?.textContent||'')+' '+p.dataset_ids.join(' ')+' '+p.project_tags.join(' ')+' '+p.note).toLowerCase();
 }
 papers.forEach(describe);
 // A saved paper annotation updates its filters and search text without reloading.
 document.addEventListener('datahunter:annotations',event=>{if(event.detail.kind!=='study')return;const i=papers.findIndex(p=>p.id===event.detail.id);if(i<0)return;papers[i]=api.paper(data.studies[i],datasets,api.current());describe(papers[i]);
  // New tags become filter options at once, as they would after a reload.
  for(const tag of papers[i].project_tags)if(!(tag in groups.project_tags[1])){const text=tag==='untagged'?'No study tags yet':tag;groups.project_tags[1][tag]=text;const input=option('project_tags',tag,text);controls.push(input);input.addEventListener('change',update);}
  update();});
 function update(){
  const state={q:search.value.trim().toLowerCase(),facets:{}};controls.filter(c=>c.checked).forEach(c=>(state.facets[c.dataset.paperFacet]||=[]).push(c.value));
  const matches=papers.filter(p=>state.q.split(/\s+/).every(w=>p.search.includes(w))&&Object.entries(state.facets).every(([k,v])=>v.some(x=>p[k].includes(x))));
  const ids=new Set(matches.map(p=>p.id)),linked=new Set(matches.flatMap(p=>p.dataset_ids));cards.forEach((card,id)=>card.hidden=!ids.has(id));
  document.querySelector('[data-study-count]').textContent=matches.length+' matching published studies / projects';
  document.querySelector('[data-paper-summary]').textContent=`${matches.length} of ${papers.length} studies · ${linked.size} unique linked dataset entries · ${matches.filter(p=>p.permission_coverage.includes('all_scopes')).length} studies with every recorded linked scope approved. Read individual scopes and conditions.`;
  api.paperFilters(state);
 }
 window.addEventListener('hashchange',()=>{const id=location.hash.slice(7);if(!location.hash.startsWith('#study-')||!papers.some(p=>p.id===id))return;search.value=id;controls.forEach(c=>c.checked=false);update();document.getElementById('study-'+id).scrollIntoView({block:'start'});});
 controls.forEach(c=>c.addEventListener('change',update));search.addEventListener('input',update);update();
})();
