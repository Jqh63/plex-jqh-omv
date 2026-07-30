function setText(id,val,cls){
  var el=document.getElementById(id);
  if(!el)return;
  el.textContent=val;
  if(cls)el.className=cls;
}

if('serviceWorker' in navigator){
  navigator.serviceWorker.getRegistration().then(function(reg){
    if(reg){
      var sw=reg.active||reg.waiting||reg.installing;
      setText('swState',sw?sw.state:'aucun',sw&&sw.state==='activated'?'ok':'');
    } else {
      setText('swState','non enregistré','warn');
    }
  }).catch(function(){setText('swState','erreur lecture','warn');});
  if(window.caches){
    caches.keys().then(function(names){
      var ours=names.filter(function(n){return n.indexOf('plex-jqh-omv')===0;});
      setText('swCache',ours.join(', ')||'aucun');
      // Version derived from the cache name via the shared parser (version.js).
      setText('appVersion',(window.pickCacheLabel&&pickCacheLabel(names))||'—');
      if(window.withCacheLabel)withCacheLabel(function(l){setText('appVersion',l);});
    });
  } else {
    setText('appVersion','—');
  }
} else {
  setText('swState','API absente','warn');
  setText('appVersion','—');
}

try {
  var cfg=JSON.parse(localStorage.getItem('plex-jqh-omv-cfg')||'{}');
  setText('cfgHost',cfg.host||'—');
  setText('cfgMac',cfg.mac?cfg.mac.match(/.{2}/g).join(':').toUpperCase():'—');
  setText('cfgPort',cfg.port||'9');
  setText('cfgApps',cfg.apps||'(défaut)');
} catch(e){
  setText('cfgHost','erreur localStorage','warn');
}

setText('navOnline',navigator.onLine?'oui':'non',navigator.onLine?'ok':'warn');
setText('displayMode',matchMedia('(display-mode:standalone)').matches?'standalone (PWA installée)':'browser');
setText('viewport',window.innerWidth+' × '+window.innerHeight);
setText('lang',navigator.language||'—');
setText('ua',navigator.userAgent);

// Paint journal (see PAINT_LOG_KEY in app.js). Newest first: the interesting
// event is always the last thing the user saw, and on a phone the top of the
// block is what is on screen without scrolling.
//
// Rendered as plain text on purpose — the page exists to be read aloud or
// screenshotted to the admin, and a monospace block survives both. A collapsed
// entry prints its first→last timestamps and its repeat count, so "green for
// 4 minutes" and "green for one tick" cannot look alike.
(function(){
  var el=document.getElementById('paintLog');
  if(!el)return;
  var hhmmss=function(ms){
    var d=new Date(ms),p=function(n){return (n<10?'0':'')+n;};
    return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
  };
  var a;
  try{a=JSON.parse(localStorage.getItem('plex-jqh-omv-paints')||'[]');}catch(e){a=null;}
  if(!Array.isArray(a)||!a.length){
    el.textContent='(vide — rouvre l\'app une fois, le journal se remplit au premier affichage)';
    return;
  }
  el.textContent=a.slice().reverse().map(function(e){
    var when=hhmmss(e.t0||e.t);
    if(e.n>1)when+='→'+hhmmss(e.t)+' ×'+e.n;
    return when+'  '+e.c+'  ← '+e.w+(e.d?'  ['+e.d+']':'');
  }).join('\n');
})();
