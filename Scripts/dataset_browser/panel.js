/* Activity panel: renders the local server's /api/panel (running scripts, study milestones, what is missing). Pure render() shared with the Node checks. */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.DataHunterPanel=api;})(typeof globalThis!=='undefined'?globalThis:this,function(){
 'use strict';
 function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
 function human(n){const u=['B','KB','MB','GB'];let i=0;n=Number(n||0);while(n>=1000&&i<u.length-1){n/=1000;i++;}return (i?n.toFixed(1):n.toFixed(0))+' '+u[i];}
 function ago(iso,now){if(!iso)return 'never';const s=(now-Date.parse(iso))/1000;if(!(s>=0))return 'just now';return s<90?Math.round(s)+' s ago':s<5400?Math.round(s/60)+' min ago':(s/3600).toFixed(1)+' h ago';}
 function bar(pct,cls,busy){return '<div class="actbar'+(busy?' busy':'')+'"><div class="fill '+cls+'" style="width:'+Math.max(0,Math.min(100,Number(pct)||0)).toFixed(1)+'%"></div></div>';}
 function processRow(p){
  const prog=p.progress;
  return '<div class="actproc"><div class="acthead"><b>'+esc(p.script)+'</b><span class="actstate">pid '+esc(p.pid)+' · running '+esc(p.elapsed)+' · cpu '+esc(p.cpu)+'%'+(p.study?' · study '+esc(p.study):'')+(p.kind==='site server'?' · site server':'')+'</span></div>'
   +(p.args?'<div class="muted"><code>'+esc(p.args)+'</code></div>':'')
   +(prog?'<div class="muted live">'+esc(prog.phase)+'</div>'+bar(prog.percent,'live',false):(p.kind==='site server'?'':bar(100,'live',true)))+'</div>';
 }
 function studyCard(s,now,siteBase){
  const steps=s.milestones.map(m=>'<li class="'+(m.done?'done':'todo')+'"><span class="mark"></span>'+esc(m.title)+'</li>').join('');
  const c=s.status_counts;
  const state=s.running?'running':(s.last_change&&(now-Date.parse(s.last_change))<180000?'changing':'idle');
  const r=s.resolver;
  return '<section class="actcard'+(state!=='idle'?' active':'')+'" id="study-'+esc(s.id)+'"><div class="acthead"><b>'+esc(s.id)+'</b><span class="actstate">'+state+' · last run change '+ago(s.last_change,now)+'</span></div>'
   +'<div class="muted">'+esc(s.name)+'</div><ul class="actsteps">'+steps+'</ul>'+bar(100*s.done/s.total,'ok',false)
   +'<div class="muted">'+s.done+' of '+s.total+' milestones · '+Number(s.held_files).toLocaleString()+' of '+Number(s.registered_files).toLocaleString()+' registered files held ('+human(s.held_bytes)+') · '+s.reports+' reports · '+s.actions+' logged actions'
   +(c?' · status: '+c.automatic+' automatic, '+c.partial+' partial, '+c.action_needed+' action needed, '+c.no_route+' no route':'')+'</div>'
   +(r?'<div class="muted live">Resolver run '+esc(r.run)+': '+esc(r.phase)+' (last event '+ago(r.last_event,now)+')</div>'+bar(r.percent,'live',false):'')
   +(s.missing&&s.missing.length?'<div class="actmissing"><b>Missing:</b> '+s.missing.map(esc).join('; ')+'</div>':'<div class="muted"><b>Nothing missing</b> for the saved request; blocked routes are on the status page.</div>')
   +'<div class="muted"><a href="'+esc(siteBase+s.site)+'status.html">status page</a> · <a href="'+esc(siteBase+s.site)+'">site</a></div></section>';
 }
 function render(model,options){
  const now=options&&options.now?options.now:Date.now();const siteBase=(options&&options.siteBase)||'/';
  const procs=(model.processes||[]).map(processRow).join('')||'<p class="muted">No Data Hunter script is running. Agent sessions show as “changing” on a study card while they write to it.</p>';
  const cards=(model.studies||[]).map(s=>studyCard(s,now,siteBase)).join('')||'<p class="muted">No study workspace yet.</p>';
  return '<p class="muted">Refreshed '+esc((model.generated_at||'').replace('T',' ').slice(0,19))+' UTC · reloads every 20 s</p><h3>Running now</h3>'+procs+'<h3>Studies</h3><div class="actcards">'+cards+'</div>';
 }
 function mount(doc,fetchImpl,setIntervalImpl){
  const host=doc.querySelector('[data-panel]');if(!host)return;
  const endpoint=host.getAttribute('data-panel-endpoint')||'/api/panel';
  const siteBase=(function(){const m=doc.location&&doc.location.pathname.match(/^(.*?\/)studies\//);return m?m[1]:'/';})();
  async function tick(){
   try{const res=await fetchImpl(endpoint,{cache:'no-store'});if(!res.ok)throw new Error('HTTP '+res.status);host.innerHTML=render(await res.json(),{siteBase});}
   catch(err){host.innerHTML='<p class="muted">The activity panel needs the local server: start it with <code>python Scripts/serve.py</code> and open the site from it. ('+esc(err&&err.message||err)+')</p>';}
  }
  tick();setIntervalImpl(tick,20000);
 }
 if(typeof document!=='undefined'&&typeof fetch==='function'){document.addEventListener('DOMContentLoaded',()=>mount(document,fetch,setInterval));}
 return {render,human,ago,mount};
});
