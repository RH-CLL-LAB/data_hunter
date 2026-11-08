/* Save generated text as a local file. No network requests. */
(function(root){
 'use strict';
 function save(text,filename,type){
  const blob=new Blob([text],{type}),url=URL.createObjectURL(blob),link=document.createElement('a');
  link.href=url;link.download=filename;link.hidden=true;
  document.body.appendChild(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
 }
 root.DataHunterSave={
  csv:(text,filename)=>save('﻿'+text,filename,'text/csv;charset=utf-8'),
  json:(value,filename)=>save(JSON.stringify(value,null,2)+'\n',filename,'application/json')
 };
})(window);
