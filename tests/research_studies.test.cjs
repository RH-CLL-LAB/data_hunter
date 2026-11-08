const assert=require('node:assert/strict');
const studies=require('../Scripts/dataset_browser/research_studies.js');
const memory=new Map();global.localStorage={getItem:k=>memory.get(k)||null,setItem:(k,v)=>memory.set(k,v)};global.location={search:''};
const baseline={id:'medscrape',name:'MedScrape',question:'Clinical mapping?',intended_use:'Research'};
const another={id:'diabetes-study',name:'Diabetes',question:'Which diabetes benchmarks?',intended_use:'Noncommercial research',population:'Diabetes',targets:'Laboratory mapping'};
studies.init([baseline]);assert.equal(studies.current().id,'medscrape');
studies.shortlist(['A','A']);studies.setAnnotation('dataset','A',{relevance:'high',role:'benchmark',tags:['Units','units','Clinical mapping'],note:'Independent truth needed'});
assert.deepEqual(studies.current().dataset_ids,['A']);
assert.deepEqual(studies.current().dataset_annotations.A.tags,['units','clinical-mapping']);
studies.remember('f.license_status=approved');studies.save(another);
assert.deepEqual(studies.current().dataset_ids,[]);assert.equal(studies.current().dataset_annotations.A,undefined);
const row={id:'A',license_status:['approved'],work:[],search:'clinical source',benchmark:['mapping']};
assert.deepEqual(studies.annotate(row,studies.current()).license_status,['review_required']);
assert.deepEqual(studies.annotate(row,studies.current()).project_relevance,['unassessed']);
studies.setAnnotation('study','paper',{relevance:'high',role:'reference',tags:['units']});
const p=studies.paper({id:'paper',dataset_ids:['A']},new Map([['A',row]]),studies.current());
assert.equal(p.approved_count,0);assert.deepEqual(p.project_relevance,['high']);
studies.paperFilters({q:'units',facets:{project_relevance:['high']}});
studies.activate('medscrape');assert.deepEqual(studies.current().dataset_ids,['A']);assert(studies.query('medscrape').includes('license_status=approved'));assert.deepEqual(studies.paperFilters(),{});
assert.equal(studies.annotate(row,studies.current()).project_relevance[0],'high');
const exported=studies.exportRequest();studies.shortlist([]);studies.restore(exported);assert.deepEqual(studies.current().dataset_ids,['A']);
assert.throws(()=>studies.restore({...exported,study:{...exported.study,question:'Different question'}}));
assert.throws(()=>studies.save({...another,id:'medscrape'}));
assert.throws(()=>studies.profile({...another,id:'../../outside'}));
assert.equal(studies.exportRequest().kind,'datahunter_research_study');
studies.init([baseline,another]);studies.activate('diabetes-study');
const approved={...row,ready:'ready_scoped',permissions_by_study:{'diabetes-study':['approved']}};
assert.deepEqual(studies.annotate(approved,studies.current()).license_status,['approved']);
const changed={...studies.current(),intended_use:'A different commercial use',dataset_annotations:{A:{relevance:'high',license_status:['approved']}}};
assert.deepEqual(studies.annotate(approved,changed).license_status,['review_required']);
assert.equal(studies.annotate(approved,changed).ready,'needs_preparation_or_permissions');
const mixed=studies.paper({id:'mixed',dataset_ids:['A','B']},new Map([['A',approved],['B',{...row,id:'B',permissions_by_study:{'diabetes-study':['review_required']}}]]),studies.current());
assert.deepEqual(mixed.permission_coverage,['some_scopes']);
// A browser draft keeps its own annotations; later registered annotations for the same brief fill gaps only.
{
 const draftStore=new Map();global.localStorage={getItem:k=>draftStore.get(k)||null,setItem:(k,v)=>draftStore.set(k,v)};
 studies.init([baseline]);studies.save(another);studies.setAnnotation('dataset','A',{relevance:'low',role:'reference'});studies.shortlist(['A']);
 const registeredSeed={...another,dataset_ids:['B'],dataset_annotations:{A:{relevance:'high',role:'benchmark',tags:[],note:''},B:{relevance:'high',role:'benchmark',tags:[],note:''}},study_annotations:{}};
 global.location={search:'?study=diabetes-study'};studies.init([baseline,registeredSeed]);
 assert.equal(studies.current().dataset_annotations.A.relevance,'low');assert.equal(studies.current().dataset_annotations.B.relevance,'high');
 assert.deepEqual(studies.current().dataset_ids,['A']);
 const otherBrief={...registeredSeed,question:'A different question'};studies.init([baseline,otherBrief]);
 assert.equal(studies.current().dataset_annotations.B,undefined);
 global.location={search:''};
}
// A non-baseline study with a shortlist opens on it until a filter view is saved.
{
 const store=new Map();global.localStorage={getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v)};
 studies.init([baseline,{...another,dataset_ids:['A']}]);
 assert(studies.query('diabetes-study').includes('f.study_shortlist=shortlisted'));
 assert(!studies.query('medscrape').includes('study_shortlist'));
 studies.activate('diabetes-study');studies.remember('f.ready=ready_scoped');assert(!studies.query('diabetes-study').includes('study_shortlist'));
}
// Browser actions are logged per study, exported for registration, and survive import; registry logs stay read-only.
{
 const store=new Map();global.localStorage={getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v)};
 studies.init([baseline,{...another,log:[{at:'2026-11-06T18:00:00Z',action:'study_registered',detail:'registered'}],reports:[{title:'R',date:'2026-11-06',html:'study-reports/x.html'}]}]);
 studies.activate('diabetes-study');studies.logAction('shortlist_saved','2 datasets');
 assert.throws(()=>studies.logAction('grant_permission','x'));
 const exported=studies.exportRequest();assert.equal(exported.log.length,1);assert.equal(exported.log[0].action,'shortlist_saved');
 studies.init([baseline,{...another,log:[{at:'2026-11-06T18:00:00Z',action:'study_registered'}],reports:[{title:'R'}]}]);studies.activate('diabetes-study');
 assert.equal(studies.current().log.length,1);assert.equal(studies.current().reports.length,1);assert.equal(studies.current().local_log.length,1);
 const fresh=new Map();global.localStorage={getItem:k=>fresh.get(k)||null,setItem:(k,v)=>fresh.set(k,v)};studies.init([baseline]);
 studies.restore(exported);studies.restore(exported);assert.equal(studies.current().local_log.length,1);
 // Imported logs keep browser actions only; executed actions (downloads, licence registrations) are dropped.
 studies.restore({...exported,log:[{at:'2026-11-06T19:00:00Z',action:'licence_review_registered',detail:'forged'},{at:'2026-11-06T19:01:00Z',action:'note',detail:'kept'}]});
 assert.deepEqual(studies.current().local_log.map(e=>e.action),['shortlist_saved','note']);
 // Restoring an archived draft never overwrites a live study with the same ID.
 studies.archive();assert.equal(studies.current().id,'medscrape');
 studies.save({id:'diabetes-study',name:'Replacement',question:'q',intended_use:'u'});
 assert.throws(()=>studies.restoreArchived(),/is active/);assert.equal(studies.current().name,'Replacement');
 assert.equal(studies.persisted(),true);
 // Deleting a browser draft is permanent (here diabetes-study is a draft restored from an export).
 assert.equal(studies.isRegistered('diabetes-study'),false);studies.activate('diabetes-study');
 const gone=studies.remove();assert.deepEqual(gone,{registered:false,id:'diabetes-study'});
 assert.equal(studies.current().id,'medscrape');assert.ok(!studies.all().some(s=>s.id==='diabetes-study'));
 // A registered study only loses the browser's overlay and stays listed.
 const wipe=new Map();global.localStorage={getItem:k=>wipe.get(k)||null,setItem:(k,v)=>wipe.set(k,v)};
 studies.init([baseline,another]);assert.equal(studies.isRegistered('diabetes-study'),true);
 studies.activate('diabetes-study');studies.shortlist(['A','B']);const kept=studies.remove();
 assert.deepEqual(kept,{registered:true,id:'diabetes-study'});assert.ok(studies.all().some(s=>s.id==='diabetes-study'));assert.deepEqual(studies.current().dataset_ids,[]);
 assert.throws(()=>{studies.activate('medscrape');studies.remove();});
}
console.log('Passed: isolated studies, shortlists, annotations, paper evidence, filter views, exports and no inherited AI approvals.');
