(function(root,factory){if(typeof module==='object'&&module.exports)module.exports=factory();else root.DataHunterStudies=factory();})(typeof window!=='undefined'?window:this,function(){
 const PROFILE_KEYS=['id','name','question','intended_use','population','targets','inclusion','exclusion'];
 function profile(value){
  if(!value||!/^[a-z0-9][a-z0-9_-]{0,79}$/.test(value.id||''))throw Error('Use a stable study ID with lowercase letters, digits, hyphens or underscores.');
  const result={id:value.id};
  for(const key of PROFILE_KEYS.slice(1)){
   const text=String(value[key]||'').trim();
   if(text.length>(key==='name'?160:4000))throw Error('Shorten the study '+key.replaceAll('_',' ')+' to '+(key==='name'?160:4000)+' characters.');
   if(['name','question','intended_use'].includes(key)&&!text)throw Error('Complete the study '+key.replaceAll('_',' ')+'.');
   result[key]=text;
  }
  return result;
 }
 function annotation(value={}){
  if(!['high','moderate','low','unassessed'].includes(value.relevance||'unassessed'))throw Error('Invalid study relevance.');
  if(!['development','benchmark','reference','exclude','unassessed'].includes(value.role||'unassessed'))throw Error('Invalid study role.');
  return {relevance:value.relevance||'unassessed',role:value.role||'unassessed',tags:[...new Set((value.tags||[]).map(t=>String(t).trim().toLowerCase().replace(/[^a-z0-9_-]+/g,'-').slice(0,60)).filter(Boolean))].slice(0,20),note:String(value.note||'').slice(0,4000)};
 }
 function annotate(row,study){
  const a=annotation(study.dataset_annotations?.[row.id]);
  const registered=seedProfiles.get(study.id),contextMatches=registered&&JSON.stringify(profile(registered))===JSON.stringify(profile(study));
  const permission=contextMatches?(row.permissions_by_study?.[study.id]||(study.id===defaultId?row.license_status:['review_required'])):['review_required'];
  const reviews=(row.manual_reviews||[]).filter(r=>(r.study_id||defaultId)===study.id&&r.applied);
  const route=reviews.length?'manual_recorded':permission?.includes('approved')?'evidence_reviewed':'manual_required';
  return {...row,license:contextMatches?(row.licenses_by_study?.[study.id]||row.license):['unknown'],research_study_id:study.id,research_study_name:study.name,baseline_ready:row.ready,
   ready:row.ready==='ready_scoped'&&!permission?.includes('approved')?'needs_preparation_or_permissions':row.ready,
   search:(row.search||'')+' '+a.tags.join(' ')+' '+a.note.toLowerCase(),license_status:permission,project_relevance:[a.relevance],project_role:[a.role],project_tags:a.tags.length?a.tags:['untagged'],review_route:[route],study_shortlist:[(study.dataset_ids||[]).includes(row.id)?'shortlisted':'not_shortlisted'],work:permission?.includes('approved')?row.work:[...new Set([...(row.work||[]),'rights'])]};
 }
 function paper(record,datasets,study){
  const a=annotation(study.study_annotations?.[record.id]),linked=record.dataset_ids.map(id=>datasets.get(id)).filter(Boolean).map(d=>annotate(d,study));
  const states=[...new Set(linked.flatMap(d=>d.license_status))];
  return {...record,project_relevance:[a.relevance],project_role:[a.role],project_tags:a.tags.length?a.tags:['untagged'],note:a.note,
   benchmark:[...new Set(linked.flatMap(d=>d.benchmark))],license_status:states.length?states:['no_dataset'],
   review_route:[...new Set(linked.flatMap(d=>d.review_route))],
   permission_coverage:[linked.length&&linked.every(d=>d.license_status.includes('approved'))?'all_scopes':linked.some(d=>d.license_status.includes('approved'))?'some_scopes':'none'],
   approved_count:linked.filter(d=>d.license_status.includes('approved')).length,dataset_count:linked.length};
 }
 let registry=null,defaultId='medscrape',activeId='medscrape',storage=null,seedProfiles=new Map(),seeds=new Map(),storageOk=true;
 const STORE='datahunter.research-studies.v1',LEGACY_STORE='medscrape.research-studies.v1';
 function readStorage(){try{return JSON.parse(localStorage.getItem(STORE)||localStorage.getItem(LEGACY_STORE)||'{}');}catch(_){return {};}}
 function persist(){try{localStorage.setItem(STORE,JSON.stringify(storage));storageOk=true;}catch(_){storageOk=false;}return storageOk;}
 // False after any failed save (quota, private mode, file:// isolation): callers warn that work is not kept.
 function persisted(){return storageOk;}
 function init(seedList,defaultStudy){
  defaultId=defaultStudy||'medscrape';activeId=defaultId;
  storage=readStorage();storage.studies=storage.studies||{};storage.views=storage.views||{};
  seedProfiles=new Map(seedList.map(s=>[s.id,profile(s)]));
  // reports and log are registry records (read-only here); local_log holds this browser's unregistered actions.
  registry=new Map(seedList.map(s=>[s.id,{...profile(s),dataset_ids:s.dataset_ids||[],dataset_annotations:s.dataset_annotations||{},study_annotations:s.study_annotations||{},reports:s.reports||[],log:s.log||[]}]));
  seeds=new Map([...registry].map(([id,s])=>[id,{...s}]));
  // A browser draft keeps its own shortlist and wins per annotation, but annotations registered
  // later for the same brief (e.g. by an intake run) still appear. A different brief never merges.
  for(const s of Object.values(storage.studies))try{
   const draft={...s,...profile(s)},seed=registry.get(s.id),same=seed&&JSON.stringify(profile(seed))===JSON.stringify(profile(draft));
   // A draft whose brief differs from the registered one shadows it; the flag lets pages say so.
   registry.set(s.id,same?{...draft,reports:seed.reports,log:seed.log,dataset_annotations:{...seed.dataset_annotations,...(s.dataset_annotations||{})},study_annotations:{...seed.study_annotations,...(s.study_annotations||{})}}:{...draft,shadowed_registered:Boolean(seed)});
  }catch(_){}
  const requested=new URLSearchParams(location.search).get('study');activeId=registry.has(requested)?requested:registry.has(storage.active)?storage.active:defaultId;
  storage.active=activeId;persist();return current();
 }
 function current(){return registry.get(activeId);}
 function all(){return [...registry.values()];}
 function save(value){
  const normalized=profile(value);
  if(normalized.id===defaultId)throw Error('Create a separate study to change the default study’s research purpose.');
  if(registry.has(normalized.id))throw Error('This study ID already exists. Use another ID for a new research question.');
  const result={...normalized,dataset_ids:[],dataset_annotations:{},study_annotations:{}};
  registry.set(result.id,result);storage.studies[result.id]=result;activeId=result.id;storage.active=activeId;
  return {study:result,persisted:persist()};
 }
 function setAnnotation(kind,id,value){
  const study=current(),key=kind==='dataset'?'dataset_annotations':'study_annotations';
  study[key]=study[key]||{};study[key][id]=annotation(value);storage.studies[study.id]=study;persist();return study[key][id];
 }
 function restore(value){
  const normalized=profile(value.study),old=registry.get(normalized.id);
  if(old&&JSON.stringify(profile(old))!==JSON.stringify(normalized))throw Error('A different research brief already uses this ID. Import it under a new ID.');
  if(!old)save(normalized);else activate(normalized.id);
  shortlist(Array.isArray(value.dataset_ids)?value.dataset_ids.filter(x=>typeof x==='string'):[]);
  for(const [kind,key] of [['dataset','dataset_annotations'],['study','study_annotations']])for(const [id,a] of Object.entries(value[key]||{}))setAnnotation(kind,id,a);
  // Keep an imported study's unregistered browser actions (deduplicated), then record the import.
  const study=current(),seen=new Set((study.local_log||[]).map(e=>e.at+e.action+(e.detail||'')));
  // Only browser-side actions can be imported; executed actions come from the registry log alone.
  const imported=(Array.isArray(value.log)?value.log:[]).filter(e=>e&&typeof e.at==='string'&&LOG_ACTIONS.includes(e.action)&&!seen.has(e.at+e.action+(e.detail||'')));
  study.local_log=[...(study.local_log||[]),...imported.map(e=>({at:e.at,action:e.action,detail:String(e.detail||'').slice(0,1000)}))].slice(-500);
  storage.studies[study.id]=study;persist();
  return current();
 }
 function shortlist(ids){const study=current();study.dataset_ids=[...new Set(ids)];storage.studies[study.id]=study;persist();return study.dataset_ids;}
 function remember(query){storage.views[activeId]=query;persist();}
 // A study with its own shortlist opens on it until the user saves another filter view.
 function query(id){
  const p=new URLSearchParams(storage.views[id]||''),study=registry.get(id);
  const filtered=[...p.keys()].some(k=>k==='q'||k==='pick'||k.startsWith('f.'));
  if(!filtered&&id!==defaultId&&study?.dataset_ids?.length)p.set('f.study_shortlist','shortlisted');
  p.set('study',id);return p.toString();
 }
 function activate(id){if(!registry.has(id))throw Error('Unknown research study.');activeId=id;storage.active=id;persist();}
 function archive(){
  if(activeId===defaultId)throw Error('The default study stays available.');
  storage.archived=storage.archived||[];storage.archived.push(current());
  delete storage.studies[activeId];registry.delete(activeId);activeId=defaultId;storage.active=activeId;persist();
 }
 function isRegistered(id){return seedProfiles.has(id);}
 // Permanently delete the active browser draft. For a registered study only the browser's own overlay goes;
 // the registered record stays until it is removed from the registry.
 function remove(){
  const id=activeId;if(id===defaultId)throw Error('The default study stays available.');
  delete storage.studies[id];delete storage.views[id];if(storage.paperViews)delete storage.paperViews[id];storage.archived=(storage.archived||[]).filter(s=>s.id!==id);
  const seed=seeds.get(id);
  if(seed)registry.set(id,seed);else{registry.delete(id);activeId=defaultId;}
  storage.active=activeId;persist();return {registered:Boolean(seed),id};
 }
 function restoreArchived(){
  const archived=storage.archived||[],value=archived.at(-1);if(!value)throw Error('No archived browser study.');
  if(registry.has(value.id))throw Error('A study with the ID “'+value.id+'” is active. Archive or rename it before restoring the archived draft.');
  archived.pop();registry.set(value.id,value);storage.studies[value.id]=value;activeId=value.id;storage.active=activeId;persist();
 }
 function paperFilters(value){storage.paperViews=storage.paperViews||{};if(value!==undefined){storage.paperViews[activeId]=value;persist();}return storage.paperViews[activeId]||{};}
 // Browser actions are logged per study and exported, so registration appends them to the study log.
 const LOG_ACTIONS=['study_created','study_imported','study_exported','shortlist_saved','annotation_saved','intake_prompt_prepared','download_instructions_prepared','file_task_prepared','file_inventory_exported','licence_review_prepared','note'];
 function logAction(action,detail=''){
  if(!LOG_ACTIONS.includes(action))throw Error('Unknown study action.');
  const study=current();study.local_log=[...(study.local_log||[]),{at:new Date().toISOString(),action,detail:String(detail).slice(0,1000)}].slice(-500);
  storage.studies[study.id]=study;persist();return study.local_log.at(-1);
 }
 function exportRequest(){return {schema_version:1,kind:'datahunter_research_study',study:profile(current()),dataset_ids:current().dataset_ids||[],dataset_annotations:current().dataset_annotations||{},study_annotations:current().study_annotations||{},log:current().local_log||[]};}
 function searchPurpose(study){return 'Research question: '+study.question+'\nIntended use: '+study.intended_use+'\nPopulation: '+study.population+'\nTargets: '+study.targets+'\nInclude: '+study.inclusion+'\nExclude: '+study.exclusion;}
 return {profile,annotation,annotate,paper,init,current,all,save,restore,setAnnotation,shortlist,remember,query,activate,archive,restoreArchived,remove,isRegistered,defaultId:()=>defaultId,paperFilters,exportRequest,searchPurpose,logAction,persisted,LOG_ACTIONS};
});
