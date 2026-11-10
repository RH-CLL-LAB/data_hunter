const assert=require('node:assert/strict');
const panel=require('../Scripts/dataset_browser/panel.js');
const now=Date.parse('2026-01-01T12:00:00Z');
const model={generated_at:'2026-01-01T11:59:50+00:00',
 processes:[{pid:7,elapsed:'00:10',cpu:'3.0',script:'resolve_full_release.py',args:'--execute',study:'demo',kind:'job',progress:{phase:'transferring 2 of 8 files',percent:25}},{pid:8,elapsed:'01:00',cpu:'0.0',script:'validate_dataset_workspace.py',args:'',study:null,kind:'job'}],
 studies:[{id:'demo',name:'Demo <study>',milestones:[{title:'Registered',done:true},{title:'Discovery run',done:false}],done:1,total:2,registered_files:8,held_files:2,held_bytes:2500,reports:1,actions:3,status_counts:{automatic:1,partial:0,action_needed:2,no_route:0},missing:['discovery not run yet'],resolver:{run:'r1',phase:'transferring 2 of 8 files',percent:25,last_event:'2026-01-01T11:59:00+00:00'},last_change:'2026-01-01T11:58:00+00:00',running:true,site:'studies/demo/docs/dataset-browser/'}]};
const html=panel.render(model,{now,siteBase:'/'});
assert(html.includes('resolve_full_release.py')&&html.includes('width:25.0%'),'a resolver process shows its real progress');
assert(html.includes('actbar busy'),'a script of unknown length shows a striped bar');
assert(html.includes('Demo &lt;study&gt;'),'names are escaped');
assert(html.includes('1 of 2 milestones')&&html.includes('2 of 8 registered files held (2.5 KB)'));
assert(html.includes('<b>Missing:</b> discovery not run yet'));
assert(html.includes('status: 1 automatic, 0 partial, 2 action needed, 0 no route'));
assert(html.includes('running · last run change 2 min ago'));
assert(html.includes('href="/studies/demo/docs/dataset-browser/status.html"'));
const empty=panel.render({generated_at:'2026-01-01T00:00:00+00:00',processes:[],studies:[]},{now});
assert(empty.includes('No Data Hunter script is running')&&empty.includes('No study workspace yet'));
assert.equal(panel.human(11565368192),'11.6 GB');assert.equal(panel.ago(null,now),'never');
console.log('Passed: activity panel rendering of processes, study cards, milestones, missing lines and the empty state.');
