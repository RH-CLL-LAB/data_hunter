/* Apply the saved appearance before paint; no external requests. */
(() => {
  const key='datahunter-theme';let saved;
  try{saved=localStorage.getItem(key);}catch(_){}
  let mode=['light','dark'].includes(saved)?saved:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
  document.documentElement.dataset.theme=mode;
  function apply(value){
    mode=value;document.documentElement.dataset.theme=mode;
    document.querySelectorAll('[data-theme-choice]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.themeChoice===mode)));
    try{localStorage.setItem(key,mode);}catch(_){}
  }
  document.addEventListener('DOMContentLoaded',()=>{
    document.querySelectorAll('[data-theme-choice]').forEach(b=>b.addEventListener('click',()=>apply(b.dataset.themeChoice)));
    apply(mode);
  });
})();
